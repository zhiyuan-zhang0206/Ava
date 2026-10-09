"""Programming errors cannot retry compaction or enter its history-trim fallback."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agent.hooks import compact
from base.host.env.agent_slices import AgentSlices
from base.lm.call import ProviderCallBinding
from base.lm.plugin_providers import build_model_catalog


@pytest.mark.parametrize("emergency", [False, True])
@pytest.mark.parametrize("provider_cause", [False, True])
async def test_compaction_programming_error_stops_once_without_history_change(
    monkeypatch: pytest.MonkeyPatch, emergency: bool, provider_cause: bool
) -> None:
    messages: list[AnyMessage] = [HumanMessage(content="Original history")]
    original = list(messages)
    calls: list[list[AnyMessage]] = []
    error = TypeError("summary implementation broke")
    llm = cast(BaseChatModel, MagicMock())
    if provider_cause:
        response = httpx2.Response(400, request=httpx2.Request("POST", "https://audit.invalid"))
        error.__cause__ = openai.BadRequestError("rejected", response=response, body=None)

    async def broken_summary(
        inputs: list[AnyMessage],
        llm: BaseChatModel,
        slices: AgentSlices,
        *,
        single_attempt: bool = False,
        binding: ProviderCallBinding | None = None,
    ) -> compact.SummaryText:
        calls.append(inputs)
        raise error

    monkeypatch.setattr(compact, "generate_summary", broken_summary)
    with pytest.raises(TypeError) as raised:
        if emergency:
            await compact.emergency_compact_summary(
                messages, llm, AgentSlices.resolve(), catalog=build_model_catalog()
            )
        else:
            await compact._auto_compact_summary(
                messages,
                llm,
                content_count=1,
                slices=AgentSlices.resolve(),
                catalog=build_model_catalog(),
            )
    assert raised.value is error
    assert len(calls) == 1
    assert messages == original


@pytest.mark.parametrize("emergency", [False, True])
@pytest.mark.parametrize("first_summary", ["", "short"])
async def test_compaction_keeps_explicit_empty_and_short_summary_recovery(
    monkeypatch: pytest.MonkeyPatch, emergency: bool, first_summary: str
) -> None:
    messages: list[AnyMessage] = [HumanMessage(content="Original history")]
    expected = "summary " * compact.COMPACT_MIN_SUMMARY_CHARS
    invocation = AsyncMock(
        side_effect=[
            (AIMessage(content=first_summary), False),
            (AIMessage(content=expected), False),
        ]
    )
    monkeypatch.setattr(compact, "ainvoke_with_cache_retry", invocation)
    llm = cast(BaseChatModel, MagicMock())
    slices = AgentSlices.resolve()
    if emergency:
        result = await compact.emergency_compact_summary(
            messages, llm, slices, catalog=build_model_catalog()
        )
    else:
        result = await compact._auto_compact_summary(
            messages, llm, 1, slices, catalog=build_model_catalog()
        )
    assert result == expected
    assert invocation.await_count == 2
    assert messages == [HumanMessage(content="Original history")]
