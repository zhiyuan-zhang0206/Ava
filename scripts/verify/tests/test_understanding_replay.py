"""The replay planner cuts a stored segment exactly where the live trigger would have."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.hooks import understanding_chunks as uc
from agent.state_channels import CompactState
from base.agents.history.checkpoint import FullHistory
from base.config import settings
from scripts.verify.understanding_replay import plan_history, plan_replay

_THRESHOLD = 1000


def _segment() -> list[AnyMessage]:
    """Head, one framework note, then 40 turns (human, ai) whose input tokens grow by 130."""
    msgs: list[AnyMessage] = [
        SystemMessage(content="head", id="h"),
        HumanMessage(content="note", id="n", additional_kwargs={"ava_msg_type": "system_note"}),
    ]
    for i in range(40):
        msgs.append(HumanMessage(content=f"u{i}", id=f"u{i}"))
        tokens = 2000 + 130 * i
        msgs.append(
            AIMessage(
                content="x",
                id=f"a{i}",
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": 1,
                    "total_tokens": tokens + 1,
                },
            )
        )
    return msgs


async def test_live_chunks_match_the_hook_turn_by_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(settings.agent, "understanding_chunk_tokens", _THRESHOLD)
    enqueued: list[tuple[int, int, str | None]] = []

    async def fake_enqueue(pool: Any, agent_id: int, **kwargs: Any) -> bool:
        chunk = kwargs["chunk"]
        enqueued.append((chunk.start_index, chunk.end_index, kwargs["end_msg_id"]))
        return True

    monkeypatch.setattr(uc, "enqueue_chunk", fake_enqueue)
    msgs = _segment()
    compact = CompactState()
    for i, msg in enumerate(msgs):
        if isinstance(msg, AIMessage):
            update = await uc.due_chunk_update(compact, msgs[:i], msg, pool=MagicMock(), agent_id=1)
            compact = update.get("compact", compact)

    planned = plan_replay(msgs, threshold=_THRESHOLD)
    live = [p for p in planned if not p.closing]
    assert len(live) > 3
    assert [(p.chunk.start_index, p.chunk.end_index, p.end_msg_id) for p in live] == enqueued


def test_the_remainder_closes_the_segment_and_an_open_tool_call_is_not_part_of_it() -> None:
    msgs = _segment()
    planned = plan_replay(msgs, threshold=10**9)
    assert [(p.chunk.start_index, p.chunk.end_index, p.closing) for p in planned] == [
        (2, len(msgs), True)
    ]
    msgs.append(AIMessage(content="", id="open", tool_calls=[{"name": "t", "args": {}, "id": "c"}]))
    assert plan_replay(msgs, threshold=10**9)[-1].chunk.end_index == len(msgs) - 1


def test_history_is_cut_segment_by_segment_with_boundary_closing_chunks() -> None:
    seg = _segment()
    head, body = cast(SystemMessage, seg[0]), seg[1:]
    history = FullHistory(
        [*body, *body, *body],
        (head, head, head),
        (0, len(body), 2 * len(body)),
    )
    planned = plan_history(history, ["b0", "b1"], threshold=_THRESHOLD)
    for k in range(3):
        mine = [p for p in planned if p.segment == k]
        assert mine and all(p.chunk.start_index >= 1 for p in mine)
        closing = [p for p in mine if p.closing]
        if k < 2:  # a boundary-closed segment ends in a closing chunk naming its checkpoint
            assert [p.boundary_checkpoint_id for p in closing] == [f"b{k}"]
        else:  # the newest segment's tail is left undescribed
            assert closing == []
    assert all(p.boundary_checkpoint_id is None for p in planned if not p.closing)
