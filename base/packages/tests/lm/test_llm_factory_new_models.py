"""Focused construction contracts for new GPT and Claude model tiers."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model


class TestGpt6Builds:
    def test_gpt6_1_sol_defaults_and_rejects_none(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        model = build_chat_model(
            "gpt-6.1-sol",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(model, ChatOpenAI)
        assert model.reasoning == {"effort": "medium", "summary": "auto"}
        model = build_chat_model(
            "gpt-6.1-sol",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(model, ChatOpenAI)
        assert model.reasoning == {"effort": "low"}
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "gpt-6.1-sol",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    def test_gpt6_astra_defaults_to_medium_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """gpt-6-astra builds on the Responses API with the OpenAI default
        effort pinned per model, like the gpt-5.6 tiers."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-6-astra",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

    @pytest.mark.parametrize("model", ("gpt-6-sol", "gpt-6-luna"))
    def test_gpt6_sol_luna_default_and_none_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none"}

        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none", "summary": "auto"}

    def test_gpt6_astra_rejects_none_and_minimal(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        for effort in ("none", "minimal"):
            monkeypatch.setattr(settings.lm, "reasoning_effort", effort)
            with pytest.raises(ValueError, match="unsupported reasoning effort"):
                build_chat_model(
                    "gpt-6-astra",
                    catalog=model_catalog,
                    llm_override=settings.lm.llm_override,
                    overrides=ModelOverrides.from_pins(
                        {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                    ),
                )

    def test_gpt6_astra_thinking_disabled_uses_minimum_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        model = build_chat_model(
            "gpt-6-astra",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(model, ChatOpenAI)
        assert model.reasoning == {"effort": "low"}


class TestClaudeAlwaysOnBuilds:
    def test_claude_sonnet_5_5_ignores_disabled_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == "high"
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_claude_opus_5_5_default_effort_and_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-opus-5-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == "medium"
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    @pytest.mark.parametrize(
        "model,effort",
        (("claude-opus-5-5", "medium"), ("claude-fable-5", "high"), ("claude-fable-5-1", "high")),
    )
    def test_claude_always_on_ignores_disabled_thinking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict[str, Any]],
        model: str,
        effort: str,
        *,
        model_catalog: ModelCatalog,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == effort
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}
        assert any(
            r["message"]
            == f"{model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
            for r in loguru_records
        )


class TestHaiku55Builds:
    def test_default_adaptive_thinking_ignores_manual_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 4096)
        llm = build_chat_model(
            "claude-haiku-5-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 128_000
        assert llm.effort == "medium"
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_disabled_thinking_is_preserved(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-5-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}
        assert llm.effort is None

    @pytest.mark.parametrize("effort", ("low", "medium", "high", "xhigh", "max"))
    def test_exact_effort_vocabulary(
        self, monkeypatch: pytest.MonkeyPatch, effort: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", effort)
        llm = build_chat_model(
            "claude-haiku-5-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == effort

    def test_manual_predecessor_none_is_not_adaptive_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "claude-haiku-5-5",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )
