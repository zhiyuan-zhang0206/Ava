"""Focused construction contracts for new GPT and Claude model tiers."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic

from shared.config import settings
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.factory import build_chat_model

ensure_provider_plugins_loaded()


class TestGpt6Builds:
    def test_gpt6_astra_defaults_to_medium_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gpt-6-astra builds on the Responses API with the OpenAI default
        effort pinned per model, like the gpt-5.6 tiers."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-6-astra")
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

    @pytest.mark.parametrize("model", ("gpt-6-sol", "gpt-6-luna"))
    def test_gpt6_sol_luna_default_and_none_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(model)
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

        m = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none"}

        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        m = build_chat_model(model)
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none", "summary": "auto"}

    def test_gpt6_astra_clamps_none_and_minimal_to_low(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT-6 Astra dropped "none" and "minimal" from the effort vocabulary
        (official guide: start at "low") — both clamp to low at build."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        for effort in ("none", "minimal"):
            monkeypatch.setattr(settings.lm, "reasoning_effort", effort)
            m = build_chat_model("gpt-6-astra")
            assert isinstance(m, ChatOpenAI)
            assert m.reasoning == {"effort": "low", "summary": "auto"}

    def test_gpt6_astra_thinking_disabled_clamps_to_low(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT-6 Astra has no off-switch for reasoning (no "none" effort), so a
        caller disabling thinking lands on the minimum rung, low, without a
        summary request."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-6-astra", thinking={"type": "disabled"})
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "low"}


class TestClaudeAlwaysOnBuilds:
    def test_claude_opus_5_5_default_effort_and_thinking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-opus-5-5")
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
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == effort
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}
        assert any(
            r["message"]
            == f"{model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
            for r in loguru_records
        )
