"""Guarded generation constructs a fresh retry-free client; normal callers keep defaults."""

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from base.config import settings
from base.lm.factory import build_chat_model


def test_openai_single_attempt_does_not_mutate_normal_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    normal = build_chat_model("gpt-6.1-sol")
    guarded = build_chat_model("gpt-6.1-sol", single_attempt=True)
    assert isinstance(normal, ChatOpenAI) and isinstance(guarded, ChatOpenAI)
    assert normal.root_async_client.max_retries == 2
    assert guarded.root_async_client.max_retries == 0
    assert normal.root_async_client is not guarded.root_async_client
    assert normal.root_async_client.max_retries == 2


def test_anthropic_single_attempt_does_not_mutate_normal_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    normal = build_chat_model("claude-sonnet-5")
    guarded = build_chat_model("claude-sonnet-5", single_attempt=True)
    assert isinstance(normal, ChatAnthropic) and isinstance(guarded, ChatAnthropic)
    assert normal.max_retries == 2
    assert guarded.max_retries == 0
    assert guarded._async_client.max_retries == 0
    assert normal._async_client.max_retries == 2


def test_undeclared_binding_fails_before_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="does not declare single-attempt"):
        build_chat_model("gemini-3.8-flash", single_attempt=True)


def test_override_cannot_satisfy_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.lm, "llm_override", "nonexistent:factory")
    with pytest.raises(ValueError, match="overrides do not declare"):
        build_chat_model("gpt-6.1-sol", single_attempt=True)


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_single_attempt_flag_is_strict(flag: object) -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        build_chat_model("gpt-6.1-sol", single_attempt=flag)  # type: ignore[arg-type]


def test_withdrawn_model_cannot_switch_provider_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    def replacement(_: str) -> str:
        return "claude-sonnet-5"

    monkeypatch.setattr("base.lm.factory.resolve_available_model", replacement)
    with pytest.raises(ValueError, match="cannot change its frozen model"):
        build_chat_model("gpt-6.1-sol", single_attempt=True)
