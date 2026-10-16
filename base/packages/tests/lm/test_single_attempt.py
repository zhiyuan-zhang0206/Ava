from collections.abc import Mapping
from typing import cast

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from base.config.tests.configuration import model_overrides
from base.lm import factory
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model


def test_openai_single_attempt_does_not_mutate_normal_client(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    normal = build_chat_model(
        "gpt-6.1-sol",
        catalog=model_catalog,
        llm_override="",
        overrides=model_overrides(),
    )
    guarded = build_chat_model(
        "gpt-6.1-sol",
        single_attempt=True,
        catalog=model_catalog,
        llm_override="",
        overrides=model_overrides(),
    )
    assert isinstance(normal, ChatOpenAI) and isinstance(guarded, ChatOpenAI)
    assert normal.root_async_client.max_retries == 2
    assert guarded.root_async_client.max_retries == 0
    assert normal.root_async_client is not guarded.root_async_client
    assert normal.root_async_client.max_retries == 2


def test_anthropic_single_attempt_does_not_mutate_normal_client(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    normal = build_chat_model(
        "claude-sonnet-5",
        catalog=model_catalog,
        llm_override="",
        overrides=model_overrides(),
    )
    guarded = build_chat_model(
        "claude-sonnet-5",
        single_attempt=True,
        catalog=model_catalog,
        llm_override="",
        overrides=model_overrides(),
    )
    assert isinstance(normal, ChatAnthropic) and isinstance(guarded, ChatAnthropic)
    assert normal.max_retries == 2
    assert guarded.max_retries == 0
    assert guarded._async_client.max_retries == 0
    assert normal._async_client.max_retries == 2


def test_undeclared_binding_fails_before_build(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="does not declare single-attempt"):
        build_chat_model(
            "gemini-3.8-flash",
            single_attempt=True,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )


def test_override_cannot_satisfy_single_attempt(*, model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="overrides do not declare"):
        build_chat_model(
            "gpt-6.1-sol",
            single_attempt=True,
            catalog=model_catalog,
            llm_override="nonexistent:factory",
            overrides=model_overrides(),
        )


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_single_attempt_flag_is_strict(flag: object, *, model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        build_chat_model(
            "gpt-6.1-sol",
            single_attempt=cast(bool, flag),
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )


def test_withdrawn_model_cannot_switch_provider_attempt(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    def replacement(_: str, *, models: Mapping[str, object]) -> str:
        assert models is model_catalog.models
        return "claude-sonnet-5"

    monkeypatch.setattr(factory, "resolve_available_model", replacement)
    with pytest.raises(ValueError, match="cannot change its frozen model"):
        build_chat_model(
            "gpt-6.1-sol",
            single_attempt=True,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
