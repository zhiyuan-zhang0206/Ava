# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""Compaction stamps its boundary, then enqueues the closing understanding chunk."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from agent.graph.hooks import compact
from agent.hooks import understanding_chunks as uc
from agent.state import AgentState
from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import Chunk
from base.config import settings


def _read_agent(field: str):
    return getattr(settings.agent, field)


class _Queue:
    """Stands in for `enqueue_chunk`: records calls, optionally refuses."""

    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, []

    async def __call__(self, pool: Any, agent_id: int, **kwargs: Any) -> bool:
        self.calls.append((agent_id, kwargs))
        return self.ok


def _request(n: int) -> list[AnyMessage]:
    return [
        SystemMessage(content="head", id="h"),
        *(HumanMessage(content=str(i), id=f"m{i}") for i in range(1, n)),
    ]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(uc, "chunk_threshold", lambda *_a, **_kw: 1000)


async def test_stamp_enqueues_the_closing_chunk_against_the_stamped_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)

    async def stamp(_pool: object, _thread_id: str, *, closing: object = None) -> str:
        return "cp-7"

    monkeypatch.setattr(compact, "mark_compact_boundary", stamp)
    state = AgentState(
        messages=_request(8), compact=CompactState(version=1, understanding_cut_index=3)
    )
    await compact.stamp_compact_boundary(MagicMock(), 9, state, read_agent=_read_agent)
    assert queue.calls == [
        (
            9,
            {
                "compact_version": 1,
                "chunk": Chunk(3, 8),
                "end_msg_id": "m7",
                "boundary_checkpoint_id": "cp-7",
            },
        )
    ]


async def test_failed_stamp_enqueues_no_closing_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue()
    monkeypatch.setattr(uc, "enqueue_chunk", queue)

    async def stamp(_pool: object, _thread_id: str, *, closing: object = None) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(compact, "mark_compact_boundary", stamp)
    await compact.stamp_compact_boundary(
        MagicMock(), 9, AgentState(messages=_request(8)), read_agent=_read_agent
    )
    assert queue.calls == []
