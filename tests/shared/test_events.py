"""Tests for shared.live_events — TokenUsage model."""

from shared.live_events import TokenUsage


def test_token_usage_has_reasoning_tokens() -> None:
    e = TokenUsage(agent_id=1, input_tokens=10, output_tokens=20, reasoning_tokens=5)
    assert e.reasoning_tokens == 5


def test_token_usage_reasoning_tokens_default_zero() -> None:
    e = TokenUsage(agent_id=1, input_tokens=1, output_tokens=1)
    assert e.reasoning_tokens == 0
