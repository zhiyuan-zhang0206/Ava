# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""The llm node carries the understanding cut on whichever command ends the turn."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph import LlmLedger, llm_node
from agent.hooks import understanding_chunks as uc
from agent.state import AgentState
from agent.state_channels import CompactState
from agent.tests._fakes import make_fake_ops_pool
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog


async def _aiter(chunks: list[AIMessageChunk]) -> AsyncIterator[AIMessageChunk]:
    for c in chunks:
        yield c


def _runtime(chunks: list[AIMessageChunk]) -> Runtime[AvaContext]:
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.astream.return_value = _aiter(chunks)
    return Runtime(
        context=AvaContext(
            ops_pool=make_fake_ops_pool(),
            llm=llm,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(),
        )
    )


def _state() -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="head", id="h"),
        HumanMessage(content="go", id="m1"),
    ]
    # Past the segment's first turn: the cut sits after the head with a 500-token baseline.
    compact = CompactState(version=3, understanding_cut_index=1, understanding_cut_tokens=500)
    return AgentState(messages=messages, halted=False, compact=compact)


def _turn(input_tokens: int, *, tool: bool) -> list[AIMessageChunk]:
    usage = UsageMetadata(input_tokens=input_tokens, output_tokens=1, total_tokens=input_tokens + 1)
    if tool:
        return [
            AIMessageChunk(
                content="ok",
                tool_call_chunks=[
                    {"name": "execute_code", "args": '{"code": "1"}', "id": "c1", "index": 0}
                ],
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata=usage,
            )
        ]
    return [
        AIMessageChunk(
            content="done",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata=usage,
        )
    ]


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)

    def threshold(
        _model: str, _overrides: ModelOverrides, _ratio: float, *, catalog: ModelCatalog
    ) -> int:
        return 1000

    monkeypatch.setattr(uc, "chunk_threshold", threshold)
    calls: list[dict] = []

    async def fake(pool: object, agent_id: int, **kwargs: object) -> bool:
        calls.append({"agent_id": agent_id, **kwargs})
        return True

    monkeypatch.setattr(uc, "enqueue_chunk", fake)
    return calls


@pytest.mark.parametrize("tool", [True, False])
async def test_cut_rides_the_tool_and_the_idle_command(
    fake_cancel_event: asyncio.Event, enqueued: list[dict], tool: bool
) -> None:
    result = await llm_node(
        _state(),
        _runtime(_turn(2500, tool=tool)),
        {"configurable": {"thread_id": "7"}},
        ledger=LlmLedger(),
    )
    assert isinstance(result, Command)
    assert result.update["compact"].understanding_cut_index == 2  # type: ignore[index]
    assert result.update["compact"].understanding_cut_tokens == 2500  # type: ignore[index]
    assert result.update["compact"].version == 3  # type: ignore[index]
    assert len(enqueued) == 1
    assert enqueued[0]["agent_id"] == 7 and enqueued[0]["compact_version"] == 3
    assert enqueued[0]["end_msg_id"] == "m1"  # the request's last message, not the reply


async def test_turn_under_the_threshold_leaves_the_state_alone(
    fake_cancel_event: asyncio.Event, enqueued: list[dict]
) -> None:
    result = await llm_node(
        _state(),
        _runtime(_turn(10, tool=True)),
        {"configurable": {"thread_id": "7"}},
        ledger=LlmLedger(),
    )
    assert "compact" not in result.update  # type: ignore[operator]
    assert enqueued == []
