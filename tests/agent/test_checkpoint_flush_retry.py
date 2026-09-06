"""A real PostgreSQL reload must observe the result after a retried final flush."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool
from typing_extensions import TypedDict

from agent.startup import _wrap_saver_writes_with_nstep_interval


class _EffectState(TypedDict):
    count: int


class _FaultSaver(AsyncPostgresSaver):
    """Inject a failed write or lost acknowledgement around the real saver."""

    failure: Literal["before_save", "after_save"] | None = None
    attempts: list[str]
    _ava_nstep_flush: Callable[[str], Awaitable[None]]

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self.attempts.append(checkpoint["id"])
        if self.failure == "before_save":
            raise RuntimeError("synthetic final checkpoint write failure")
        saved = await super().aput(config, checkpoint, metadata, new_versions)
        if self.failure == "after_save":
            raise RuntimeError("synthetic final checkpoint acknowledgement failure")
        return saved


@pytest.mark.parametrize("failure", [None, "before_save", "after_save"])
async def test_final_flush_retry_is_durable_and_idempotent(
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    failure: Literal["before_save", "after_save"] | None,
) -> None:
    saver = _FaultSaver(aops_pool)
    saver.attempts = []
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, 4)
    flush = saver._ava_nstep_flush
    entered, release = asyncio.Event(), asyncio.Event()
    effect_file = tmp_path / "synthetic-effects"

    async def effect(state: _EffectState) -> _EffectState:
        with effect_file.open("a") as output:
            output.write("effect\n")
        return {"count": state["count"] + 1}

    async def blocked(state: _EffectState) -> _EffectState:
        entered.set()
        await release.wait()
        return state

    def retained_boundary(state: _EffectState) -> _EffectState:
        return state

    builder = StateGraph(_EffectState)
    builder.add_node("retained_boundary", retained_boundary)  # pyright: ignore[reportUnknownMemberType]
    builder.add_node("effect", effect)  # pyright: ignore[reportUnknownMemberType]
    builder.add_node("blocked", blocked)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "retained_boundary")
    builder.add_edge("retained_boundary", "effect")
    builder.add_edge("effect", "blocked")
    builder.add_edge("blocked", END)
    graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
    thread_id = str(uuid4())
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    invocation = asyncio.create_task(graph.ainvoke({"count": 0}, config))  # pyright: ignore[reportUnknownMemberType]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
    assert invocation.cancelled()
    assert effect_file.read_text().splitlines() == ["effect"]

    # This independently constructed graph cannot see the wrapper's in-memory
    # tail. The completed effect is initially absent from its durable snapshot.
    cold_graph = builder.compile(checkpointer=AsyncPostgresSaver(aops_pool))  # pyright: ignore[reportUnknownMemberType]
    assert (await cold_graph.aget_state(config)).values["count"] == 0
    saver.attempts.clear()
    saver.failure = failure
    if failure is not None:
        with pytest.raises(RuntimeError, match="synthetic final checkpoint"):
            await flush(thread_id)
        expected = 1 if failure == "after_save" else 0
        assert (await cold_graph.aget_state(config)).values["count"] == expected

    saver.failure = None
    await flush(thread_id)
    await flush(thread_id)
    assert (await cold_graph.aget_state(config)).values["count"] == 1
    assert len(saver.attempts) == (1 if failure is None else 2)
    assert len(set(saver.attempts)) == 1  # Retry upserts the same checkpoint.
    assert effect_file.read_text().splitlines() == ["effect"]
