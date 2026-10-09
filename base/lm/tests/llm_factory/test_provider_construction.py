"""Tests for base/lm/factory.py build_chat_model prefix dispatch.

Does not hit API — only verifies the contract: "given the correct model name,
the factory returns the corresponding provider class + correct base_url / api_key
configuration".

deepseek-* uses ChatAnthropic + DeepSeek anthropic-compatible endpoint
(no longer using langchain-deepseek).
"""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import (
    build_chat_model,
)


class TestProviderConstruction:
    def test_gpt_branch_builds(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        from langchain_openai import ChatOpenAI

        assert isinstance(m, ChatOpenAI)

    def test_gpt_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "openai_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            build_chat_model(
                "gpt-5.6-sol",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_gpt_enables_responses_api_reasoning_summary(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """gpt-* must go through the Responses API with an explicit reasoning
        effort + summary — Chat Completions returns zero reasoning, and the
        model's default effort is too low to emit a summary, so effort must be
        set. summary='auto' surfaces the reasoning summary as a `reasoning`
        content block (folded to the canonical `thinking` shape downstream)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

    def test_gpt_rejects_unsupported_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "minimal")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "gpt-5.6-sol",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_gpt_thinking_disabled_drops_to_effort_none(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """thinking={'type':'disabled'} (short-text paths) → effort 'none', no
        summary requested. Symmetric with gemini include_thoughts=False and the
        deepseek effort skip: a caller disabling thinking gets no reasoning."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none"}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """mimo-* returns ReasoningContentChatModel (not bare ChatOpenAI) — the
        subclass recovers the `reasoning_content` delta that the base drops.
        base_url + api-key header target the Xiaomi OpenAI-compatible endpoint."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "xiaomimimo.com" in str(m.openai_api_base)

    def test_mimo_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MIMO_API_KEY"):
            build_chat_model(
                "mimo-v2.5-pro",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_kimi_returns_chat_moonshot(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """kimi-* (Moonshot) returns ChatMoonshot from langchain-moonshot.
        Reasoning streams in `additional_kwargs["reasoning_content"]` — the
        streaming fan-out and timeline handle both styles."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatMoonshot)

    def test_kimi_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            build_chat_model(
                "kimi-k3",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_unknown_prefix_raises(self, *, model_catalog: ModelCatalog) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            build_chat_model(
                "llama-3",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_unknown_prefix_error_hints_at_adding_branch(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        """The error message should indicate which prefix branch to add — the caller
        can know how to expand without reading the factory source code."""
        with pytest.raises(ValueError) as exc_info:
            build_chat_model(
                "mistral-large",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )
        assert "mistral-*" in str(exc_info.value)

    def test_glm_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """glm-* (Zhipu) returns ReasoningContentChatModel pointed at the
        Zhipu OpenAI-compatible endpoint — GLM 5.2 streams its thinking in the
        `reasoning_content` delta, which the subclass recovers into thinking blocks."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_5_3_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """glm-5.3-flash dispatches through the same glm branch — Zhipu
        OpenAI-compatible endpoint, ReasoningContentChatModel."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.3-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "zhipu_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GLM_API_KEY"):
            build_chat_model(
                "glm-5.2",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_qwen_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """qwen* (Alibaba DashScope) returns ReasoningContentChatModel pointed at
        the default public compatible-mode endpoint — Qwen streams its thinking in
        the `reasoning_content` delta, which the subclass recovers into thinking
        blocks."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_qwen3_8_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """qwen3.8-flash dispatches through the same qwen branch — DashScope
        compatible-mode endpoint, ReasoningContentChatModel."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert m.stream_usage is True

    def test_qwen_honors_the_configured_base_url(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A dedicated Model Studio workspace serves the same API on its own host,
        which the public default cannot reach at all — so the endpoint has to be
        config, not a constant. Hardcoding it locked those accounts out entirely."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        workspace = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        monkeypatch.setattr(settings.lm, "dashscope_base_url", workspace)
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == workspace

    def test_qwen_requests_stream_usage(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """stream_usage sends `stream_options.include_usage`, without which the
        stream carries no final usage frame at all — and DashScope reports its
        implicit context-cache hits in that frame
        (`prompt_tokens_details.cached_tokens`). Drop it and every qwen turn
        bills as a full cache miss."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.stream_usage is True

    def test_qwen_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "dashscope_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    # ── per-model default streaming ─────────────────────────────────────

    def test_kimi_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """kimi-* models default to streaming=True — _consume_llm's
        fatal-provider-error fallback automatically retries non-streaming on
        engine_overloaded_error (K3 measured: streaming ~40% 429 → non-streaming
        0% 429), so the construction-time default streams for progressive display."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False

    def test_kimi_k2_7_code_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """kimi-k2.7-code should also default to streaming — same provider, same
        _consume_llm fallback. Model-level granularity lets us add or remove
        individual models without changing the prefix-wide logic."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k2.7-code",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """mimo-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_glm_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """glm-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_qwen_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """qwen* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_claude_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """claude-* carries no registry streaming opt-out → default streaming=True.
        Verifying the Anthropic path separately since it uses a different
        constructor (ChatAnthropic)."""

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        m = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_deepseek_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """deepseek-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        m = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_gemini_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """gemini-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is False

    def test_gpt_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """gpt-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.disable_streaming is False

    def test_explicit_streaming_false_on_non_kimi(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Explicit streaming=False overrides the model default (True for
        non-Kimi). The caller should be able to force non-streaming
        regardless of the model catalog."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            streaming=False,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is True

    def test_explicit_streaming_true_overrides_kimi_default(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Explicit streaming=True overrides the Kimi default (False).
        A caller that knows the endpoint is healthy can opt back in."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            streaming=True,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False
