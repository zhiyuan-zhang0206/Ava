"""Compaction calls preserve their prefix, deadline and failure authority."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.llm import execute_code
from agent.llm.invoke import ainvoke_tool_call
from base.config import settings
from base.lm.errors import is_retryable_provider_error


async def test_tool_call_preserves_complete_prefix() -> None:
    response = AIMessage(content="summary")
    runnable = MagicMock(ainvoke=AsyncMock(return_value=response))
    llm = MagicMock(bind_tools=MagicMock(return_value=runnable))
    system = SystemMessage(content="stable head")
    messages: list[AnyMessage] = [system, HumanMessage(content="history")]

    assert await ainvoke_tool_call(cast(BaseChatModel, llm), messages) is response
    llm.bind_tools.assert_called_once_with([execute_code])
    runnable.ainvoke.assert_awaited_once_with(messages)
    assert runnable.ainvoke.call_args.args[0][0] is system


async def test_compaction_deadline_cancels_model(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = asyncio.Event()

    async def hanging(_messages: object) -> AIMessage:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        return AIMessage(content="unreachable")

    runnable = MagicMock(ainvoke=AsyncMock(side_effect=hanging))
    llm = MagicMock(bind_tools=MagicMock(return_value=runnable))
    monkeypatch.setattr(settings.lm, "llm_compact_timeout_seconds", 0.01)
    with pytest.raises(TimeoutError):
        await ainvoke_tool_call(cast(BaseChatModel, llm), [HumanMessage(content="history")])
    assert cancelled.is_set()
    assert runnable.ainvoke.await_count == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_failure_and_cancellation_propagate_once(cancel: bool) -> None:
    failure = asyncio.CancelledError() if cancel else TypeError("model code failed")
    runnable = MagicMock(ainvoke=AsyncMock(side_effect=failure))
    llm = MagicMock(bind_tools=MagicMock(return_value=runnable))
    with pytest.raises(type(failure)) as caught:
        await ainvoke_tool_call(cast(BaseChatModel, llm), [HumanMessage(content="history")])
    assert caught.value is failure
    assert runnable.ainvoke.await_count == 1


async def test_transport_error_is_normalized_for_caller_retry() -> None:
    failure = httpx.ConnectError("wire lost")
    runnable = MagicMock(ainvoke=AsyncMock(side_effect=failure))
    llm = MagicMock(bind_tools=MagicMock(return_value=runnable))
    with pytest.raises(Exception) as caught:
        await ainvoke_tool_call(cast(BaseChatModel, llm), [HumanMessage(content="history")])
    assert caught.value.__cause__ is failure
    assert is_retryable_provider_error(caught.value)
    assert runnable.ainvoke.await_count == 1
