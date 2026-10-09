"""Actual SDK events retain the service receipt through LangChain conversion."""

import pytest
from anthropic.types import Message, RawMessageDeltaEvent, RawMessageStartEvent
from langchain_core.messages import AIMessage

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic
from base.lm.factory import build_chat_model, build_chat_model_bound
from base.lm.usage import usage_model


def _message(speed: str) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "answer"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10, "speed": speed},
        }
    )


@pytest.mark.parametrize("speed", ["fast", "standard"])
def test_nonstream_receipt_survives_output_conversion(
    monkeypatch: pytest.MonkeyPatch, speed: str, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model, binding = build_chat_model_bound(
        "claude-opus-5-5-fast",
        catalog=model_catalog,
        llm_override=settings.lm.llm_override,
        overrides=ModelOverrides.from_pins(
            {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
        ),
    )
    assert isinstance(model, ThinkingTokensChatAnthropic)
    assert binding is model_catalog.bindings["claude-"]
    result = model._format_output(_message(speed))
    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert message.response_metadata["speed"] == speed
    expected = "claude-opus-5-5-fast" if speed == "fast" else "claude-opus-5-5"
    assert usage_model(message, "claude-opus-5-5-fast", catalog=model_catalog) == expected


@pytest.mark.parametrize("speed", ["fast", "standard"])
def test_stream_receipt_is_emitted_once_when_chunks_are_combined(
    monkeypatch: pytest.MonkeyPatch, speed: str, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_chat_model(
        "claude-opus-5-5-fast",
        catalog=model_catalog,
        llm_override=settings.lm.llm_override,
        overrides=ModelOverrides.from_pins(
            {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
        ),
    )
    assert isinstance(model, ThinkingTokensChatAnthropic)
    start = RawMessageStartEvent(type="message_start", message=_message(speed))
    end = RawMessageDeltaEvent.model_validate(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 100, "output_tokens": 10, "speed": speed},
        }
    )
    first, _ = model._make_message_chunk_from_anthropic_event(start, coerce_content_to_string=False)
    last, _ = model._make_message_chunk_from_anthropic_event(end, coerce_content_to_string=False)
    assert first is not None and last is not None
    combined = first + last
    assert combined.response_metadata["speed"] == speed
    assert "speed" not in last.response_metadata
    expected = "claude-opus-5-5-fast" if speed == "fast" else "claude-opus-5-5"
    assert usage_model(combined, "claude-opus-5-5-fast", catalog=model_catalog) == expected
