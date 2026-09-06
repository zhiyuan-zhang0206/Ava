"""N-step checkpoint write throttling keeps checkpoint rows and writes in lockstep."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.constants import PUSH
from typing_extensions import TypedDict

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
