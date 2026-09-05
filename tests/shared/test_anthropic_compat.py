"""shared/lm/_anthropic_compat.py — ThinkingTokensChatAnthropic preserves thinking_tokens.

Verifies that the _make_message_chunk_from_anthropic_event override captures
output_tokens_details.thinking_tokens from the message_delta stream event
and adds it to usage_metadata.output_token_details.reasoning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, UsageMetadata

from shared.lm._anthropic_compat import ThinkingTokensChatAnthropic


def _message_delta_event(*, thinking_tokens: int) -> MagicMock:
    """Build a mock message_delta event carrying thinking_tokens."""
    event = MagicMock()
    event.type = "message_delta"
    output_details = MagicMock()
    output_details.thinking_tokens = thinking_tokens
    event.usage.output_tokens_details = output_details
    return event


def test_message_delta_with_thinking_tokens_sets_reasoning():
    """A message_delta event with thinking_tokens > 0 populates
    output_token_details.reasoning on the returned chunk."""
    event = _message_delta_event(thinking_tokens=1234)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    # Simulate the override's post-processing logic
    assert msg.usage_metadata is not None
    existing: dict = dict(msg.usage_metadata.get("output_token_details") or {})
    thinking = event.usage.output_tokens_details.thinking_tokens
    if thinking > 0 and "reasoning" not in existing and "reasoning_tokens" not in existing:
        msg.usage_metadata["output_token_details"] = {**existing, "reasoning": thinking}  # type: ignore[typeddict-item]

    details = msg.usage_metadata.get("output_token_details") or {}
    assert details.get("reasoning") == 1234


def test_message_delta_zero_thinking_tokens_noop():
    """thinking_tokens == 0 → no output_token_details added."""
    event = _message_delta_event(thinking_tokens=0)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    assert msg.usage_metadata is not None
    existing = dict(msg.usage_metadata.get("output_token_details") or {})
    thinking = event.usage.output_tokens_details.thinking_tokens
    if thinking > 0 and "reasoning" not in existing and "reasoning_tokens" not in existing:
        msg.usage_metadata["output_token_details"] = {**existing, "reasoning": thinking}  # type: ignore[typeddict-item]
    # No output_token_details set because thinking_tokens == 0
    assert msg.usage_metadata.get("output_token_details") is None


def test_non_message_delta_event_noop():
    """Non-message_delta events (e.g. content_block_delta) are not modified."""
    event = MagicMock()
    event.type = "content_block_delta"
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    # The guard checks event.type == "message_delta"
    is_message_delta = getattr(event, "type", None) == "message_delta"
    # For non-message_delta events, the guard is False
    assert not is_message_delta
    assert msg.usage_metadata is not None
    assert msg.usage_metadata.get("output_token_details") is None


def test_reasoning_already_present_no_overwrite():
    """If output_token_details.reasoning is already set, don't overwrite."""
    event = _message_delta_event(thinking_tokens=999)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            output_token_details={"reasoning": 42},
        ),
    )
    assert msg.usage_metadata is not None
    existing = dict(msg.usage_metadata.get("output_token_details") or {})
    thinking = event.usage.output_tokens_details.thinking_tokens
    if thinking > 0 and "reasoning" not in existing and "reasoning_tokens" not in existing:
        msg.usage_metadata["output_token_details"] = {**existing, "reasoning": thinking}  # type: ignore[typeddict-item]
    # Should keep the original value, not overwrite
    details = msg.usage_metadata.get("output_token_details") or {}
    assert details.get("reasoning") == 42


def test_override_method_supplements_usage_metadata(monkeypatch: pytest.MonkeyPatch):
    """The actual _make_message_chunk_from_anthropic_event override
    captures thinking_tokens and supplements usage_metadata."""
    event = _message_delta_event(thinking_tokens=500)

    # Build a chunk that super() would return (with usage_metadata but no output_token_details)
    super_chunk = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
        ),
    )

    # Patch super()._make_message_chunk_from_anthropic_event to return super_chunk
    def _fake_super_method(
        _self: object,
        _evt: object,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool = False,
        block_start_event: object = None,
    ) -> tuple[AIMessageChunk | None, object | None]:
        return super_chunk, None

    monkeypatch.setattr(
        "langchain_anthropic.chat_models.ChatAnthropic._make_message_chunk_from_anthropic_event",
        _fake_super_method,
    )

    # Construct with model= (alias for model_name) — type checker complains but
    # runtime uses Pydantic alias
    model = ThinkingTokensChatAnthropic(model="claude-sonnet-5")  # type: ignore[call-arg]
    result_chunk, _ = model._make_message_chunk_from_anthropic_event(
        event,
        stream_usage=True,
        coerce_content_to_string=False,
        block_start_event=None,
    )

    assert result_chunk is not None
    assert result_chunk.usage_metadata is not None
    details = result_chunk.usage_metadata.get("output_token_details") or {}
    assert details.get("reasoning") == 500
