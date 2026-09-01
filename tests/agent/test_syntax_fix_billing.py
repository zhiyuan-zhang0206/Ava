"""Billing emission coverage for the syntax-repair provider call."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage


async def test_successful_syntax_repair_call_emits_chat_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful repair response reaches the shared billing emitter.

    The regression this catches is a rare provider call in the syntax repair
    fallback being omitted from the per-call ledger.
    """
    from ava_builtins.plugins.ava_syntax_fix._llm_repair import _repair_once

    emitted: list[tuple[AIMessage, dict[str, Any]]] = []
    response = AIMessage(
        content="fixed = True",
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )

    class _LLM:
        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return response

    def _emit(message: AIMessage, **kwargs: Any) -> None:
        emitted.append((message, kwargs))

    monkeypatch.setattr("shared.lm.billing.emit_billing_from_message", _emit)

    assert await _repair_once(_LLM(), []) == "fixed = True"
    assert len(emitted) == 1
    assert emitted[0][0] is response
    assert emitted[0][1]["model"] == "deepseek-v4-flash"
    assert emitted[0][1]["usage_kind"] == "chat"
