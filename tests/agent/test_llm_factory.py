"""Tests for shared/lm/factory.py build_chat_model prefix dispatch.

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

from shared.config import settings
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.factory import _resolve_override, build_chat_model, validate_model_config
from shared.lm.registry import SUPPORTED_MODELS, resolve_setting

ensure_provider_plugins_loaded()


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
        llm = build_chat_model("claude-opus-4-7")
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
        llm = build_chat_model("deepseek-v4-pro")
        assert isinstance(llm, ChatAnthropic)
        # base_url uses DeepSeek instead of the official Anthropic
        assert "deepseek.com" in str(llm.anthropic_api_url)
        # The plugin key is independent from ANTHROPIC_API_KEY.
        assert llm.anthropic_api_key.get_secret_value() == "sk-test-deepseek"

    def test_deepseek_sets_max_tokens_to_model_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deepseek-* must explicitly set max_tokens — langchain-anthropic's model profile
        only covers claude-*, giving deepseek-* a fallback legacy default of 4096, and extended
        thinking can easily exceed 4096 in a single turn and be truncated (agent 169 incident).
        Set to DeepSeek V4 Pro documented cap of 384K so the client is no longer the bottleneck;
        setting a high max_tokens has no side effect — max_tokens is the server-side output cap,
        not a budget, and the model only generates the tokens it needs."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model("deepseek-v4-pro")
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
        """claude models not registered in the registry (MODELS) with a max_output_tokens
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
        llm = build_chat_model("deepseek-v4-pro")
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs

    def test_deepseek_default_is_max(self) -> None:
        """DeepSeek's per-model registry default is 'max' — DeepSeek only automatically
        upgrades to max for recognized harnesses (docs note Claude Code / OpenCode), Ava is not
        on that list, so we must explicitly request it. Changing this default will break this
        test, signaling the need to sync docs / runbook."""
        assert resolve_setting("reasoning_effort", model="deepseek-v4-pro") == "max"
        assert resolve_setting("reasoning_effort", model="deepseek-v4-flash") == "max"

    def test_deepseek_max_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """effort='max' → injects output_config.effort=max into extra_body, passed through
        by langchain-anthropic to the Anthropic SDK into the POST body."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("deepseek-v4-pro")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_high_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """high is also a valid effort value — DeepSeek docs high/max two tiers;
        explicit value overrides the per-model 'max' from the registry."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("deepseek-v4-pro")
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
        llm = build_chat_model("deepseek-v4-flash")
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
            "deepseek-v4-flash", thinking={"type": "enabled", "budget_tokens": 8000}
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8000}

    def test_deepseek_out_of_range_effort_clamps_to_model_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A level outside the model's `effort_levels` clamps like every other
        provider branch instead of riding to the wire raw — the whole point of the
        clamp is that a config value explodes (or bends) at build time, not as a
        provider 400 after the agent is already running."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        llm = build_chat_model("deepseek-v4-pro")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "high"}}

    def test_shipped_web_fetch_config_builds_an_accepted_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pair `ava.web.fetch` ships with (AVA_WEB_FETCH_MODEL=deepseek-v4-flash,
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
            build_chat_model("deepseek-v4-pro")

    def test_deepseek_thinking_disabled_skips_extra_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'disabled'} should not inject output_config.effort even if the
        resolved effort is non-empty — DeepSeek server rejects setting both simultaneously (400
        "thinking options type cannot be disabled when reasoning_effort is set"). The labeler
        short-text path explicitly disables thinking; the global env effort must not sneak back in."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("deepseek-v4-pro", thinking={"type": "disabled"})
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
            "deepseek-v4-pro", thinking={"type": "enabled", "budget_tokens": 8000}
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit reasoning_effort parameter overrides the resolved effort —
        allowing a caller like syntax repair to lock on max without being dragged down
        by a global config set to a lower effort by some agent."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("deepseek-v4-pro", reasoning_effort="max")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_when_global_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When global effort is empty, explicit override still injects — the override is an
        independent source, not dependent on the global being non-empty."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model("deepseek-v4-pro", reasoning_effort="max")
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing DEEPSEEK_API_KEY raises RuntimeError fail-fast, rather than silently falling
        back to ANTHROPIC_API_KEY and only discovering the issue through a 401."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            build_chat_model("deepseek-v4-pro")

    def test_claude_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing ANTHROPIC_API_KEY raises RuntimeError fail-fast — consistent with all other
        provider branches. Previously claude-* lacked this check; ChatAnthropic with no key
        silently hung, the agent process stuck in the LLM call never returning."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            build_chat_model("claude-opus-4-7")

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

    def test_gpt_effort_clamps_unsupported_vocabulary_onto_model_rungs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gpt-5.6's wire vocabulary has no "minimal" (official docs: none,
        low, medium, high, xhigh, max) — an explicit out-of-vocabulary effort
        clamps to the nearest supported rung instead of reaching the wire."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "minimal")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-5.6-sol")
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "low", "summary": "auto"}

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

    def test_gpt6_astra_clamps_none_and_minimal_to_low(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gpt-6 dropped "none" and "minimal" from the effort vocabulary
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
        """gpt-6 has no off-switch for reasoning (no "none" effort), so a
        caller disabling thinking lands on the minimum rung, low, without a
        summary request."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model("gpt-6-astra", thinking={"type": "disabled"})
        assert isinstance(m, ChatOpenAI)
        assert m.reasoning == {"effort": "low"}

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

    def test_mimo_returns_reasoning_content_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """mimo-* returns ReasoningContentChatModel (not bare ChatOpenAI) — the
        subclass recovers the `reasoning_content` delta that the base drops.
        base_url + api-key header target the Xiaomi OpenAI-compatible endpoint."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro")
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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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

    def test_mimo_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """mimo-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_glm_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """glm-* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-test")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("glm-5.2")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_qwen_defaults_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """qwen* carries no registry streaming opt-out → default streaming=True."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

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

        m = build_chat_model("deepseek-v4-pro")
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


class TestReasoningEffortDispatch:
    """Per-provider injection / clamping / gating tests for AVA_REASONING_EFFORT.

    Across provider vocabularies none/minimal/low/medium/high/xhigh/max, _clamp_effort
    maps to what each provider truly accepts: out-of-range values clamp to the nearest
    tier (ties round up, matching the precedent where DeepSeek server maps low/medium→high,
    xhigh→max); misspelled values explode at build time instead of landing as a provider 400.
    """

    def test_clamp_unknown_value_raises(self) -> None:
        from shared.lm.factory import _clamp_effort

        with pytest.raises(ValueError, match="unknown reasoning effort"):
            _clamp_effort("higth", ("low", "high"), target="test")

    def test_clamp_ties_round_up(self) -> None:
        """medium is equidistant from low/high → picks high; xhigh is equidistant from
        high/max → picks max."""
        from shared.lm.factory import _clamp_effort

        assert _clamp_effort("medium", ("low", "high", "max"), target="test") == "high"
        assert _clamp_effort("xhigh", ("low", "high", "max"), target="test") == "max"
        assert _clamp_effort("none", ("low", "medium", "high"), target="test") == "low"

    # ── claude ──────────────────────────────────────────────────────────

    def test_claude_effort_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sonnet-5 supports effort → uses ChatAnthropic's effort field
        (on the wire it is output_config.effort)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "xhigh")
        llm = build_chat_model("claude-sonnet-5")
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == "xhigh"

    def test_claude_empty_effort_not_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model("claude-sonnet-5")
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_haiku_effort_field_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """haiku-4-5 does not support ChatAnthropic's `effort` field (server 400) — that
        knob is ignored rather than passed through and causing an error. AVA_REASONING_EFFORT
        itself is not completely ignored — it instead maps to the thinking budget
        mapping (see TestReasoningEffortDispatch's test_haiku_high_effort_opts_in_at_default_budget)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_thinking_disabled_skips_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """caller explicitly disables thinking (labeler/judge short-text path) → do not
        inject effort, aligning with the deepseek branch: the global env should not sneak
        reasoning back in."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model("claude-sonnet-5", thinking={"type": "disabled"})
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_haiku_thinking_budget_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """haiku-4-5 defaults thinking OFF; when AVA_CLAUDE_THINKING_BUDGET_TOKENS>0,
        injects thinking={'type':'enabled','budget_tokens':N} so the haiku agent really thinks."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}

    def test_haiku_budget_zero_leaves_thinking_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_thinking_wins_over_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """caller explicitly passes thinking (e.g. labeler's disabled) always overrides
        the budget config."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model("claude-haiku-4-5-20251001", thinking={"type": "disabled"})
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    def test_haiku_high_effort_opts_in_at_default_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """haiku-4-5 has no `effort` field (server 400) but AVA_REASONING_EFFORT
        still does something: clamped onto the model's ('none','high') binary,
        'high' opts extended thinking in at the fallback default budget — this
        is the only lever available when claude_thinking_budget_tokens is unset,
        so the spawn-UI effort dropdown isn't inert for this model."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}
        assert llm.effort is None

    def test_haiku_none_effort_leaves_thinking_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_budget_wins_over_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """settings.lm.claude_thinking_budget_tokens (an explicit numeric
        budget) always wins over the effort-derived default — an operator who
        tuned the budget directly shouldn't have a spawn-time effort pick
        silently override it to the generic 8192 fallback."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 20_000)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model("claude-haiku-4-5-20251001")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 20_000}

    def test_sonnet_ignores_thinking_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On adaptive-thinking models (sonnet-5), enabled+budget_tokens is a server
        400 — budget config only acts on extended-thinking-only models (haiku).
        Ignoring the budget does not leave thinking unset: the branch still sends the
        adaptive default with display='summarized'."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model("claude-sonnet-5")
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_claude_defaults_to_summarized_display(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every adaptive-thinking claude model must opt into display='summarized'.

        The server default is display='omitted': the model thinks, but the wire
        returns only a signature with no thinking text — no thinking_delta
        in the stream and an empty thinking block in the committed message, so the
        timeline has nothing to render. haiku-4-5 (extended-thinking-only) is NOT in
        this list — it 400s on type='adaptive'."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        for model in (
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-fable-5-1",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
        ):
            llm = build_chat_model(model)
            assert isinstance(llm, ChatAnthropic)
            assert llm.thinking == {"type": "adaptive", "display": "summarized"}, model

    def test_adaptive_caller_thinking_gets_display_filled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller-passed adaptive config without display gets summarized filled in."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-sonnet-5", thinking={"type": "adaptive"})
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_caller_display_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller that explicitly chose display='omitted' keeps it — the factory
        only fills the field when absent."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5", thinking={"type": "adaptive", "display": "omitted"}
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "omitted"}

    def test_claude_thinking_disabled_no_adaptive_injection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking={'type':'disabled'} (labeler/judge short-text path) passes through
        untouched — no adaptive default, no display added."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model("claude-sonnet-5", thinking={"type": "disabled"})
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    # ── gemini ──────────────────────────────────────────────────────────

    def test_gemini_effort_maps_to_thinking_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash")
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "low"

    def test_gemini_max_clamps_to_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gemini's thinking_level vocabulary only goes up to high — max/xhigh clamp to high."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash")
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "high"

    def test_gemini_default_leaves_thinking_level_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """effort empty → thinking_level=None → model default tier (Flash medium / Pro high)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash")
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level is None

    def test_gemini_thinking_disabled_drops_to_minimal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The semantics of disabled changed from "turn off visibility" to "truly lower the tier":
        only disabling include_thoughts still causes the model to think and bill, so we must also
        set thinking_level='minimal' to be a cost switch."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model("gemini-3.5-flash", thinking={"type": "disabled"})
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "minimal"
        assert m.include_thoughts is False

    # ── kimi ────────────────────────────────────────────────────────────

    def test_kimi_effort_injected_via_extra_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kimi-k3 defaults max (most expensive) and is non-streaming — effort is the
        only downgrade knob, via the top-level reasoning_effort body field (extra_body channel)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3")
        assert isinstance(m, ChatMoonshot)
        assert m.extra_body == {"reasoning_effort": "low"}

    def test_kimi_medium_clamps_to_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kimi enum is low/high/max — medium is equidistant, ties round up to high."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "medium")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3")
        assert isinstance(m, ChatMoonshot)
        assert m.extra_body == {"reasoning_effort": "high"}

    def test_kimi_thinking_disabled_sends_no_thinking_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """K3 cannot disable thinking and does not accept the K2.x thinking parameter —
        a disabled request logs a warning and ignores, passing nothing (passing would 400)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model("kimi-k3", thinking={"type": "disabled"})
        assert isinstance(m, ChatMoonshot)
        assert m.thinking is None
        assert m.extra_body is None

    # ── glm ─────────────────────────────────────────────────────────────

    def test_glm_effort_injected_as_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """glm-5.2 defaults max — reasoning_effort is an OpenAI standard payload field,
        directly using ChatOpenAI's declared field (stuffing into model_kwargs is rejected
        by langchain)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("glm-5.2")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "high"

    def test_glm_5_3_low_effort_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GLM-5.3 documents low as a native reasoning-effort rung."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("glm-5.3")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "low"

    def test_glm_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GLM natively supports disabling thinking (body top-level thinking.type=disabled) —
        previously silently swallowed (F5). disabled also skips effort injection (caller wants
        the money-saving path)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("glm-5.2", thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
        assert m.reasoning_effort is None

    @pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
    def test_glm_5_3_thinking_disabled_warns_instead_of_sending_body(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """GLM-5.3 / GLM-5.3-Flash always think: the endpoint rejects
        thinking.type=disabled with a 400 (error code 1210, live-checked
        2026-08-27), so the builder drops the disabled body and warns (the kimi
        branch's pattern) instead of sending a body that fails every call."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    # ── qwen ────────────────────────────────────────────────────────────

    def test_qwen_none_effort_disables_thinking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DashScope's compatible-mode endpoint has no graded effort field — its
        graded knob is a token budget (`thinking_budget`) and the OpenAI-standard
        `reasoning_effort` string is documented only for its Responses API, which
        Ava does not bind. So the cross-provider knob clamps onto the binary
        none/high and 'none' lands on the endpoint's own off-switch."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}
        # never the OpenAI-standard field: this endpoint would ignore or 400 it
        assert m.reasoning_effort is None

    def test_qwen_graded_effort_clamps_onto_the_on_rung(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A graded level clamps onto 'high', which IS the model's own default
        (thinking already on) — so nothing is sent rather than a level the
        endpoint has no field for."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None

    def test_qwen_thinking_disabled_sends_enable_thinking_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller disabling thinking (the labeler / judge short-text path) wins
        over a global effort that would otherwise leave reasoning on."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("qwen3.8-max", thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}

    def test_qwen_unknown_effort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd effort fails fast at build time, not as a provider 400."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "hihg")
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            build_chat_model("qwen3.8-max")

    # ── mimo ────────────────────────────────────────────────────────────

    def test_mimo_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MiMo officially documents thinking.type enabled/disabled — disabled goes through the
        body top-level thinking (previously silently swallowed, F5). effort is not mentioned
        in the official reference, so it is not connected."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro", thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    def test_mimo_high_effort_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MiMo has no graded reasoning_effort field — 'max' clamps to the
        two-value ('none', 'high') table's 'high' tier, which is the provider
        default (thinking already on) and needs no extra_body at all."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    def test_mimo_none_effort_disables_thinking_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AVA_REASONING_EFFORT='none' clamps to the table's 'none' tier — the
        only tier that differs from the provider default — and maps onto the
        same body thinking.type=disabled switch as an explicit thinking arg."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    def test_mimo_low_effort_clamps_to_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """low sits equidistant from none/high in the cross-provider vocab —
        clamp ties round up, so it lands on 'high' (provider default, no-op),
        not 'none' (which would silently disable thinking)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None

    def test_mimo_explicit_thinking_disabled_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller-explicit thinking={'type':'disabled'} (short-text paths) wins
        outright — reasoning_effort is not even consulted."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from shared.lm._reasoning_compat import ReasoningContentChatModel

        m = build_chat_model("mimo-v2.5-pro", thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}


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


class TestResolveOverride:
    """`_resolve_override` reports resolution errors in layers: format / module / factory / return type
    raises ValueError / ImportError / AttributeError / TypeError respectively —
    letting the user see from stderr which step failed without grepping the factory source code."""

    def test_no_colon_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("just_a_module_no_colon", "claude-opus-4-7")

    def test_empty_module_path_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override(":factory", "claude-opus-4-7")

    def test_invalid_factory_identifier_raises_value_error(self) -> None:
        """factory segment is not a valid Python identifier (empty / has spaces / starts with digit)
        → ValueError. If isidentifier check is missed, getattr with an invalid name behaves
        unpredictably (depends on Python internals) — must raise early."""
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:has space", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:1starts_with_digit", "claude-opus-4-7")

    def test_module_not_found_raises_import_error(self) -> None:
        """module path not found → ImportError, message indicates the override."""
        with pytest.raises(ImportError, match="cannot find module"):
            _resolve_override("definitely_does_not_exist_pkg_xyz.module:factory", "claude-opus-4-7")

    def test_factory_attribute_missing_raises_attribute_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """module found but factory_name attribute not defined → AttributeError.
        Hints that the module path is correct but the factory name is misspelled / not exported."""
        _install_fake_module(monkeypatch, "tests._llm_override_empty")
        with pytest.raises(AttributeError, match="has no attribute 'missing_factory'"):
            _resolve_override("tests._llm_override_empty:missing_factory", "claude-opus-4-7")

    def test_factory_returns_non_basechatmodel_raises_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """factory returns non-BaseChatModel subclass → TypeError. Prevents fake factory
        from returning a string / dict that later graph code chokes on with AttributeError,
        which is hard to locate."""
        mod = _install_fake_module(monkeypatch, "tests._llm_override_bad_return")
        mod.build = lambda _model: "not a chat model"  # type: ignore[attr-defined]
        with pytest.raises(TypeError, match="factory returned 'str', not a BaseChatModel"):
            _resolve_override("tests._llm_override_bad_return:build", "claude-opus-4-7")

    def test_success_returns_factory_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """factory returns a BaseChatModel subclass instance → _resolve_override passes through.
        The model parameter should be fed to the factory as-is (factory decides whether to use it)."""
        captured_model: list[str] = []
        mod = _install_fake_module(monkeypatch, "tests._llm_override_ok")

        def build(model: str) -> _FakeLLM:
            captured_model.append(model)
            return _FakeLLM()

        mod.build = build  # type: ignore[attr-defined]
        result = _resolve_override("tests._llm_override_ok:build", "claude-opus-4-7")
        assert isinstance(result, _FakeLLM)
        assert captured_model == ["claude-opus-4-7"]

    def test_build_chat_model_respects_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When `AVA_LLM_OVERRIDE` env is set, `build_chat_model` short-circuits through
        `_resolve_override` — bypassing claude-* / deepseek-* prefix dispatch,
        allowing e2e tests / debugging to inject a fake LLM (without hitting the real API)."""
        mod = _install_fake_module(monkeypatch, "tests._llm_override_e2e")
        mod.build = lambda _model: _FakeLLM()  # type: ignore[attr-defined]
        monkeypatch.setattr(settings.lm, "llm_override", "tests._llm_override_e2e:build")
        llm = build_chat_model("claude-opus-4-7")
        assert isinstance(llm, _FakeLLM)

    def test_build_chat_model_override_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When override resolution fails, `build_chat_model` must not silently fall back
        to the original prefix dispatch — if env is set it must take effect; failure must
        be loud, telling the user to correct the env rather than letting prod silently use
        the real LLM."""
        monkeypatch.setattr(settings.lm, "llm_override", "bad_format_no_colon")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            build_chat_model("claude-opus-4-7")


class TestValidateModelConfig:
    """Spawn-time validation tests for validate_model_config.

    Does not run build_chat_model — only verifies config validity checks on the
    spawn path, including whether the model name is registered and whether the API key
    is configured.
    """

    # --- helper -----------------------------------------------------------------

    @staticmethod
    def _clear_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
        for attr in (
            "anthropic_api_key",
            "deepseek_api_key",
            "gemini_api_key",
            "openai_api_key",
            "xiaomi_api_key",
            "moonshot_api_key",
            "zhipu_api_key",
            "dashscope_api_key",
        ):
            monkeypatch.setattr(settings.lm, attr, None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("MIMO_API_KEY", raising=False)

    @staticmethod
    def _set_plugin_keys(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.runtime_config.read_env_aliases",
            lambda: {
                "ANTHROPIC_API_KEY": "sk-test",
                "DEEPSEEK_API_KEY": "sk-test",
                "GEMINI_API_KEY": "sk-test",
                "OPENAI_API_KEY": "sk-test",
                "GLM_API_KEY": "sk-test",
                "DASHSCOPE_API_KEY": "sk-test",
                "MOONSHOT_API_KEY": "sk-test",
                "MIMO_API_KEY": "sk-test",
            },
        )

    # --- model resolution -------------------------------------------------------

    def test_model_from_config_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model takes precedence over the cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="claude-sonnet-5",
            config={"llm_model": "deepseek-v4-pro"},
        )
        assert result == "deepseek-v4-pro"

    def test_fallback_to_cluster_default_when_config_omits_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config doesn't have llm_model, use the cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-v4-pro",
            config={"some_other_key": "value"},
        )
        assert result == "deepseek-v4-pro"

    def test_fallback_to_cluster_default_when_config_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config=None, use the cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(model="deepseek-v4-pro", config=None)
        assert result == "deepseek-v4-pro"

    def test_no_model_configured_raises(self) -> None:
        """Neither cluster default nor config has a model → ValueError."""
        with pytest.raises(ValueError, match="no model configured"):
            validate_model_config(model=None, config=None)

    def test_no_model_configured_empty_config(self) -> None:
        """Empty config and model=None → ValueError."""
        with pytest.raises(ValueError, match="no model configured"):
            validate_model_config(model=None, config={})

    # --- model name validation --------------------------------------------------

    def test_unknown_model_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """model name not in SUPPORTED_MODELS → ValueError."""
        with pytest.raises(ValueError, match="unknown model 'not-a-real-model'"):
            validate_model_config(model="not-a-real-model")

    def test_known_model_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registered model → returns model name."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(model="deepseek-v4-pro")
        assert result == "deepseek-v4-pro"

    def test_all_supported_models_pass_name_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every model in SUPPORTED_MODELS passes the name check."""
        self._set_plugin_keys(monkeypatch)
        all_models = [
            m
            for models in __import__(
                "shared.lm.factory", fromlist=["SUPPORTED_MODELS"]
            ).SUPPORTED_MODELS.values()
            for m in models
        ]
        for m in all_models:
            # Only testing name existence, not key (key validation is separate)
            self._set_plugin_keys(monkeypatch)
            result = validate_model_config(model=m)
            assert result == m

    def test_superseded_model_stays_spawn_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Supersession is display-only: a registry model carrying
        ``superseded_by`` (hidden from the picker) must keep passing spawn
        validation — settings/config switching back to it stays allowed."""
        from dataclasses import replace

        from shared.lm.registry import MODELS

        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        monkeypatch.setitem(MODELS, "glm-5.2", replace(MODELS["glm-5.2"], superseded_by="kimi-k3"))
        result = validate_model_config(model="glm-5.2", config={"llm_model": "glm-5.2"})
        assert result == "glm-5.2"

    # --- API key validation -----------------------------------------------------

    def test_missing_claude_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(model="claude-sonnet-5")

    def test_missing_deepseek_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEEPSEEK_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            validate_model_config(model="deepseek-v4-pro")

    def test_missing_gemini_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GEMINI_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            validate_model_config(model="gemini-3.5-flash")

    def test_missing_openai_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            validate_model_config(model="gpt-5.6-sol")

    def test_missing_mimo_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MIMO_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="MIMO_API_KEY"):
            validate_model_config(model="mimo-v2.5-pro")

    def test_missing_kimi_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MOONSHOT_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
            validate_model_config(model="kimi-k3")

    def test_missing_glm_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GLM_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="GLM_API_KEY"):
            validate_model_config(model="glm-5.2")

    def test_missing_qwen_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DASHSCOPE_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
            validate_model_config(model="qwen3.8-max")

    def test_key_present_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """key is set → validation passes."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "shared.runtime_config.read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(model="claude-sonnet-5")
        assert result == "claude-sonnet-5"

    def test_config_model_with_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model points to a provider with missing key → ValueError (not the cluster default's key)."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "shared.runtime_config.read_env_aliases",
            lambda: {
                "DEEPSEEK_API_KEY": "sk-test",
                "GEMINI_API_KEY": "sk-test",
            },
        )
        # The cluster default has its plugin key, but config picks claude → fail.
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(
                model="deepseek-v4-pro",  # cluster default
                config={"llm_model": "claude-sonnet-5"},  # per-agent override
            )

    def test_config_model_with_key_present_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model's provider key is set → passes. The cluster default is irrelevant."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "shared.runtime_config.read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(
            model="deepseek-v4-pro",  # cluster default (has no deepseek key)
            config={"llm_model": "claude-sonnet-5"},  # per-agent (has key)
        )
        assert result == "claude-sonnet-5"

    def test_config_with_non_string_model_ignores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model is not a str → ignored, fallback to cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-v4-pro",
            config={"llm_model": 42},  # not a string
        )
        assert result == "deepseek-v4-pro"

    def test_override_skips_api_key_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AVA_LLM_OVERRIDE is set → skip API key check. e2e tests depend on this behavior."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "llm_override", "tests.fakes:build")
        result = validate_model_config(model="claude-sonnet-5")
        assert result == "claude-sonnet-5"

    def test_restored_model_validates_to_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gemini 3.8 is spawnable again (2026-09-06 user order): validation
        resolves it to itself, not to the 3.7 fallback."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)

        result = validate_model_config(model="gemini-3.8-flash")

        assert result == "gemini-3.8-flash"


class TestThinkingDisabledAcrossRoster:
    """issue #190: `thinking={"type": "disabled"}` must be expressible — or a
    no-op — for every model in the supported roster, never a 400. This is the
    assertion whose absence let gemini-2.5-flash / gemini-3.8-flash become
    silently unusable as labeler_model."""

    _ALL_KEY_FIELDS = (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "dashscope_api_key",
    )

    def _stub_all_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for attr in self._ALL_KEY_FIELDS:
            monkeypatch.setattr(settings.lm, attr, SecretStr("sk-test"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GLM_API_KEY", "sk-test")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")

    @pytest.mark.parametrize("model", [m for models in SUPPORTED_MODELS.values() for m in models])
    def test_roster_model_constructs_with_thinking_disabled(
        self, model: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every spawnable model can be constructed with thinking disabled — a
        provider that cannot express it must no-op, not 400."""
        self._stub_all_keys(monkeypatch)
        llm = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(llm, BaseChatModel)

    def test_gemini_2_5_thinking_disabled_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
    ) -> None:
        """gemini-2.5-flash has no thinking_level vocabulary: disabled must not
        put thinking parameters on the wire (the 400 of issue #190) and must
        warn instead of silently dropping the request."""
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model("gemini-2.5-flash", thinking={"type": "disabled"})
        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.thinking_level is None
        assert llm.include_thoughts is None
        assert any(
            "gemini-2.5-flash" in r["message"] and "ignored" in r["message"] for r in loguru_records
        )

    def test_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gemini 3.1 rejects `minimal`, so disabled thinking maps to its
        lowest declared level while retaining no thought blocks on the wire."""
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model("gemini-3.1-pro-preview", thinking={"type": "disabled"})
        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.thinking_level == "low"
        assert llm.include_thoughts is False
