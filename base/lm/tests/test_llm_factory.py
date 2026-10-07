"""Tests for base/lm/factory.py build_chat_model prefix dispatch.

Does not hit API — only verifies the contract: "given the correct model name,
the factory returns the corresponding provider class + correct base_url / api_key
configuration".

deepseek-* uses ChatAnthropic + DeepSeek anthropic-compatible endpoint
(no longer using langchain-deepseek).
"""

from __future__ import annotations

import sys
import types

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult
from pydantic import SecretStr

from base.config import settings
from base.lm.factory import (
    build_chat_model,
)
from base.lm.plugin_providers import model_catalog
from base.lm.registry import resolve_setting

model_catalog()


class TestBuildChatModel:
    def test_restored_gemini_3_8_flash_builds_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 2026-09-06 user order restored 3.8 to the production picker;
        the builder resolves it to itself, not to the 3.7 fallback."""
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model("gemini-3.8-flash")

        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.model == "gemini-3.8-flash"

    def test_claude_prefix_returns_chat_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-opus-5")
        assert isinstance(llm, ChatAnthropic)
        assert llm.anthropic_api_key.get_secret_value() == "sk-ant-test"

    def test_claude_sonnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-sonnet-5")
        assert isinstance(llm, ChatAnthropic)

    def test_deepseek_returns_chat_anthropic_with_deepseek_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deepseek-* also returns ChatAnthropic, but base_url points to DeepSeek
        anthropic-compatible endpoint, and its plugin reads DEEPSEEK_API_KEY."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model("deepseek-flash")
        assert isinstance(llm, ChatAnthropic)
        # base_url uses DeepSeek instead of the official Anthropic
        assert "deepseek.com" in str(llm.anthropic_api_url)
        # The plugin key is independent from ANTHROPIC_API_KEY.
        assert llm.anthropic_api_key.get_secret_value() == "sk-test-deepseek"

    def test_deepseek_sets_max_tokens_to_model_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deepseek-* must explicitly set max_tokens — langchain-anthropic's model profile
        only covers claude-*, giving deepseek-* a fallback legacy default of 4096, and extended
        thinking can easily exceed 4096 in a single turn and be truncated (agent 169 incident).
        Set to DeepSeek Flash documented cap of 384K so the client is no longer the bottleneck;
        setting a high max_tokens has no side effect — max_tokens is the server-side output cap,
        not a budget, and the model only generates the tokens it needs."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model("deepseek-flash")
        # isinstance narrow enables pyright to see ChatAnthropic.max_tokens
        # (build_chat_model returns BaseChatModel, the parent doesn't have this field)
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000

    def test_claude_sets_max_tokens_from_explicit_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude-* now explicitly pins max_tokens (_CLAUDE_MAX_TOKENS) just like deepseek —
        langchain-anthropic 1.4.4's profile table didn't include claude-sonnet-5, falling back
        to legacy 4096; thinking tokens count toward max_tokens and guaranteed truncation
        (same failure mode as #169)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-sonnet-5")
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 128_000

    def test_claude_haiku_max_tokens_is_64k(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """haiku-4-5's official output cap is 64K (not 128K) — per-model table, not
        a prefix-shared constant, prevents a small-cap model from borrowing a large cap
        and hitting a server 400."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 64_000

    def test_unregistered_claude_model_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claude models not registered in the registry (model_catalog().models) with a max_output_tokens
        raise immediately — do not fall back to langchain's stale profile (unknown id gives 4096)
        which would borrow the wrong cap."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with pytest.raises(ValueError, match="Unknown claude model"):
            build_chat_model("claude-sonnet-3-9")

    def test_deepseek_empty_effort_skips_extra_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit settings.lm.reasoning_effort="" → does not inject output_config, endpoint
        defaults (medium thinking budget). Empty string is an explicit non-None value that
        overrides the per-model "max" from the registry — opt out to a cheaper tier."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model("deepseek-flash")
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs

    def test_deepseek_default_is_max(self) -> None:
        """DeepSeek's per-model registry default is 'max' — DeepSeek only automatically
        upgrades to max for recognized harnesses (docs note Claude Code / OpenCode), Ava is not
        on that list, so we must explicitly request it. Changing this default will break this
        test, signaling the need to sync docs / runbook."""
        assert resolve_setting("reasoning_effort", model="deepseek-flash") == "max"

    def test_deepseek_max_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """effort='max' → injects output_config.effort=max into extra_body, passed through
        by langchain-anthropic to the Anthropic SDK into the POST body."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("deepseek-flash")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_high_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """high is also a valid effort value — DeepSeek docs high/max two tiers;
        explicit value overrides the per-model 'max' from the registry."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("deepseek-flash")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "high"}}

    def test_deepseek_none_effort_disables_thinking_instead_of_wire_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """effort='none' must never reach `output_config.effort` — DeepSeek's wire
        vocabulary is graded levels only and 400s on it ("unknown variant `none`,
        expected one of high, low, medium, max, xhigh"), which is what took every
        `ava.web.fetch` down (AVA_WEB_FETCH_REASONING ships as "none"). Off is the
        endpoint's thinking switch, which is also what the setting promises."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model("deepseek-flash")
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "disabled"}

    def test_deepseek_none_effort_leaves_caller_thinking_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that passed `thinking` stated its own intent and wins over a
        global effort of 'none' — the effort is dropped rather than overwriting the
        caller's thinking config."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model(
            "deepseek-flash", thinking={"type": "enabled", "budget_tokens": 8000}
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8000}

    def test_deepseek_rejects_unsupported_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model("deepseek-flash")

    def test_shipped_web_fetch_config_builds_an_accepted_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pair `ava.web.fetch` ships with (AVA_WEB_FETCH_MODEL=deepseek-flash,
        AVA_WEB_FETCH_REASONING=none) has to build a request the endpoint accepts —
        that exact pair is what 400'd on every fetch in production."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model(
            settings.web.web_fetch_model, reasoning_effort=settings.web.web_fetch_reasoning
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "disabled"}

    def test_deepseek_unknown_effort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd effort fails fast at build time rather than as a provider 400."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "nonw")
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            build_chat_model("deepseek-flash")

    def test_deepseek_thinking_disabled_skips_extra_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'disabled'} should not inject output_config.effort even if the
        resolved effort is non-empty — DeepSeek server rejects setting both simultaneously (400
        "thinking options type cannot be disabled when reasoning_effort is set"). The labeler
        short-text path explicitly disables thinking; the global env effort must not sneak back in."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("deepseek-flash", thinking={"type": "disabled"})
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs

    def test_deepseek_thinking_enabled_still_injects_extra_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'enabled', ...} does not conflict — the server accepts thinking enabled
        together with reasoning_effort. Only thinking=disabled is mutually exclusive with effort."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model(
            "deepseek-flash", thinking={"type": "enabled", "budget_tokens": 8000}
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit reasoning_effort parameter overrides the resolved effort —
        allowing a caller like syntax repair to lock on max without being dragged down
        by a global config set to a lower effort by some agent."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("deepseek-flash", reasoning_effort="max")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_when_global_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When global effort is empty, explicit override still injects — the override is an
        independent source, not dependent on the global being non-empty."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model("deepseek-flash", reasoning_effort="max")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing DEEPSEEK_API_KEY raises RuntimeError fail-fast, rather than silently falling
        back to ANTHROPIC_API_KEY and only discovering the issue through a 401."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            build_chat_model("deepseek-flash")

    def test_claude_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing ANTHROPIC_API_KEY raises RuntimeError fail-fast — consistent with all other
        provider branches. Previously claude-* lacked this check; ChatAnthropic with no key
        silently hung, the agent process stuck in the LLM call never returning."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            build_chat_model("claude-opus-5")

    def test_gemini_branch_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        m = build_chat_model("gemini-3.1-pro-preview")
        from langchain_google_genai import ChatGoogleGenerativeAI

        assert isinstance(m, ChatGoogleGenerativeAI)

    def test_gemini_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            build_chat_model("gemini-3.1-pro-preview")

    def test_gemini_enables_include_thoughts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gemini-* must set include_thoughts=True — otherwise the model still thinks but returns
        zero thought blocks (live view zero reasoning). When enabled, thoughts are returned as
        `{"type":"thinking","thinking":...}` content blocks, same shape as claude/deepseek,
        reusing the existing streaming/timeline path without a provider branch."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash")
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.include_thoughts is True

    def test_gemini_thinking_disabled_drops_include_thoughts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'disabled'} (short-text path) → include_thoughts=False,
        no thought blocks returned. Symmetric with deepseek thinking-disabled skipping effort
        injection: the caller explicitly disables reasoning, so thinking should not be emitted."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash", thinking={"type": "disabled"})
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.include_thoughts is False

    def test_gemini_media_args_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Media-path extras (ava.understand): media_resolution maps onto the
        Google enum, media_thinking_level wins over the resolved effort, and
        base_url overrides the endpoint. include_thoughts stays at the SDK
        default (None) — the media path never surfaced thought blocks."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from google.genai.types import MediaResolution
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            media_resolution="high",
            media_thinking_level="low",
            base_url="http://localhost:8080/v1beta",
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.media_resolution == MediaResolution.MEDIA_RESOLUTION_HIGH
        assert m.thinking_level == "low"
        assert m.base_url == "http://localhost:8080/v1beta"  # type: ignore[reportUnknownMemberType]
        assert m.include_thoughts is None

    def test_gemini_media_resolution_maps_each_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from google.genai.types import MediaResolution
        from langchain_google_genai import ChatGoogleGenerativeAI

        for setting, enum in [
            ("low", MediaResolution.MEDIA_RESOLUTION_LOW),
            ("medium", MediaResolution.MEDIA_RESOLUTION_MEDIUM),
            ("high", MediaResolution.MEDIA_RESOLUTION_HIGH),
        ]:
            m = build_chat_model("gemini-3.5-flash", media_resolution=setting)
            assert isinstance(m, ChatGoogleGenerativeAI)
            assert m.media_resolution == enum

    def test_gemini_invalid_media_resolution_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        with pytest.raises(ValueError, match="media_resolution"):
            build_chat_model("gemini-3.5-flash", media_resolution="ultra")

    def test_gpt_branch_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        m = build_chat_model("gpt-5.6-sol")
        from langchain_openai import ChatOpenAI

        assert isinstance(m, ChatOpenAI)

    def test_gpt_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "openai_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            build_chat_model("gpt-5.6-sol")

    def test_gpt_enables_responses_api_reasoning_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gpt-* must go through the Responses API with an explicit reasoning
        effort + summary — Chat Completions returns zero reasoning, and the
        model's default effort is too low to emit a summary, so effort must be
        set. summary='auto' surfaces the reasoning summary as a `reasoning`
        content block (folded to the canonical `thinking` shape downstream)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-5.6-sol")
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

    def test_gpt_rejects_unsupported_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "minimal")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model("gpt-5.6-sol")

    def test_gpt_thinking_disabled_drops_to_effort_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'disabled'} (short-text paths) → effort 'none', no
        summary requested. Symmetric with gemini include_thoughts=False and the
        deepseek effort skip: a caller disabling thinking gets no reasoning."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-5.6-sol", thinking={"type": "disabled"})
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "none"}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """mimo-* returns ReasoningContentChatModel (not bare ChatOpenAI) — the
        subclass recovers the `reasoning_content` delta that the base drops.
        base_url + api-key header target the Xiaomi OpenAI-compatible endpoint."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model)
        assert isinstance(m, ReasoningContentChatModel)
        assert "xiaomimimo.com" in str(m.openai_api_base)

    def test_mimo_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MIMO_API_KEY"):
            build_chat_model("mimo-v2.5-pro")

    def test_kimi_returns_chat_moonshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kimi-* (Moonshot) returns ChatMoonshot from langchain-moonshot.
        Reasoning streams in `additional_kwargs["reasoning_content"]` — the
        streaming fan-out and timeline handle both styles."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3")
        assert isinstance(m, ChatMoonshot)

    def test_kimi_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            build_chat_model("kimi-k3")

    def test_unknown_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            build_chat_model("llama-3")

    def test_unknown_prefix_error_hints_at_adding_branch(self) -> None:
        """The error message should indicate which prefix branch to add — the caller
        can know how to expand without reading the factory source code."""
        with pytest.raises(ValueError) as exc_info:
            build_chat_model("mistral-large")
        assert "mistral-*" in str(exc_info.value)

    def test_glm_returns_reasoning_content_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """glm-* (Zhipu) returns ReasoningContentChatModel pointed at the
        Zhipu OpenAI-compatible endpoint — GLM 5.2 streams its thinking in the
        `reasoning_content` delta, which the subclass recovers into thinking blocks."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("glm-5.2")
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_5_3_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """glm-5.3-flash dispatches through the same glm branch — Zhipu
        OpenAI-compatible endpoint, ReasoningContentChatModel."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("glm-5.3-flash")
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "zhipu_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GLM_API_KEY"):
            build_chat_model("glm-5.2")

    def test_qwen_returns_reasoning_content_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """qwen* (Alibaba DashScope) returns ReasoningContentChatModel pointed at
        the default public compatible-mode endpoint — Qwen streams its thinking in
        the `reasoning_content` delta, which the subclass recovers into thinking
        blocks."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_qwen3_8_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """qwen3.8-flash dispatches through the same qwen branch — DashScope
        compatible-mode endpoint, ReasoningContentChatModel."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-flash")
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert m.stream_usage is True

    def test_qwen_honors_the_configured_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dedicated Model Studio workspace serves the same API on its own host,
        which the public default cannot reach at all — so the endpoint has to be
        config, not a constant. Hardcoding it locked those accounts out entirely."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        workspace = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        monkeypatch.setattr(settings.lm, "dashscope_base_url", workspace)
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == workspace

    def test_qwen_requests_stream_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stream_usage sends `stream_options.include_usage`, without which the
        stream carries no final usage frame at all — and DashScope reports its
        implicit context-cache hits in that frame
        (`prompt_tokens_details.cached_tokens`). Drop it and every qwen turn
        bills as a full cache miss."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.stream_usage is True

    def test_qwen_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "dashscope_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            build_chat_model("qwen3.8-max")

    # ── per-model default streaming ─────────────────────────────────────

    def test_kimi_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kimi-* models default to streaming=True — _consume_llm's
        fatal-provider-error fallback automatically retries non-streaming on
        engine_overloaded_error (K3 measured: streaming ~40% 429 → non-streaming
        0% 429), so the construction-time default streams for progressive display."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3")
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False

    def test_kimi_k2_7_code_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kimi-k2.7-code should also default to streaming — same provider, same
        _consume_llm fallback. Model-level granularity lets us add or remove
        individual models without changing the prefix-wide logic."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k2.7-code")
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
        """mimo-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model)
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_glm_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """glm-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("glm-5.2")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_qwen_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """qwen* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_claude_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claude-* carries no registry streaming opt-out → default streaming=True.
        Verifying the Anthropic path separately since it uses a different
        constructor (ChatAnthropic)."""
        from langchain_anthropic import ChatAnthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        m = build_chat_model("claude-sonnet-5")
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_deepseek_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deepseek-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from langchain_anthropic import ChatAnthropic

        m = build_chat_model("deepseek-flash")
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_gemini_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gemini-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash")
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is False

    def test_gpt_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gpt-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-5.6-sol")
        assert isinstance(m, ChatOpenAI)
        assert m.disable_streaming is False

    def test_explicit_streaming_false_on_non_kimi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit streaming=False overrides the model default (True for
        non-Kimi). The caller should be able to force non-streaming
        regardless of the model catalog."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash", streaming=False)
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is True

    def test_explicit_streaming_true_overrides_kimi_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit streaming=True overrides the Kimi default (False).
        A caller that knows the endpoint is healthy can opt back in."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3", streaming=True)
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False


class _FakeLLM(BaseChatModel):
    """Minimal stub that passes the BaseChatModel isinstance check — _resolve_override
    success path must return a BaseChatModel subclass. At runtime LangChain won't
    actually call _generate; it's only used for type checking at the build_chat_model
    exit point."""

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, *_args: object, **_kwargs: object) -> ChatResult:
        raise NotImplementedError


def _install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str) -> types.ModuleType:
    """Inject a fake module into sys.modules so that importlib.import_module can find it —
    cleaner than writing a real module to disk and cleaning up; monkeypatch automatically
    restores."""
    mod = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


class _SpyClient:
    """Provider-client stand-in recording `close()` calls (sync or async)."""

    def __init__(self, *, async_close: bool = False) -> None:
        self.closed = 0
        self._async_close = async_close

    def close(self) -> object:
        if self._async_close:
            return self._aclose()
        self.closed += 1
        return None

    async def _aclose(self) -> None:
        self.closed += 1


__all__ = ["_FakeLLM", "_SpyClient"]
