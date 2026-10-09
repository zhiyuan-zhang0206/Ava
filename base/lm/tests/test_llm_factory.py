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

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import (
    build_chat_model,
)
from base.lm.registry import resolve_setting


class TestBuildChatModel:
    def test_restored_gemini_3_8_flash_builds_itself(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """The 2026-09-06 user order restored 3.8 to the production picker;
        the builder resolves it to itself, not to the 3.7 fallback."""
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model(
            "gemini-3.8-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )

        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.model == "gemini-3.8-flash"

    def test_claude_prefix_returns_chat_anthropic(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-opus-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.anthropic_api_key.get_secret_value() == "sk-ant-test"

    def test_claude_sonnet(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)

    def test_deepseek_returns_chat_anthropic_with_deepseek_base_url(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """deepseek-* also returns ChatAnthropic, but base_url points to DeepSeek
        anthropic-compatible endpoint, and its plugin reads DEEPSEEK_API_KEY."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        # base_url uses DeepSeek instead of the official Anthropic
        assert "deepseek.com" in str(llm.anthropic_api_url)
        # The plugin key is independent from ANTHROPIC_API_KEY.
        assert llm.anthropic_api_key.get_secret_value() == "sk-test-deepseek"

    def test_deepseek_sets_max_tokens_to_model_cap(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """deepseek-* must explicitly set max_tokens — langchain-anthropic's model profile
        only covers claude-*, giving deepseek-* a fallback legacy default of 4096, and extended
        thinking can easily exceed 4096 in a single turn and be truncated (agent 169 incident).
        Set to DeepSeek Flash documented cap of 384K so the client is no longer the bottleneck;
        setting a high max_tokens has no side effect — max_tokens is the server-side output cap,
        not a budget, and the model only generates the tokens it needs."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        # isinstance narrow enables pyright to see ChatAnthropic.max_tokens
        # (build_chat_model returns BaseChatModel, the parent doesn't have this field)
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000

    def test_claude_sets_max_tokens_from_explicit_table(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """claude-* now explicitly pins max_tokens (_CLAUDE_MAX_TOKENS) just like deepseek —
        langchain-anthropic 1.4.4's profile table didn't include claude-sonnet-5, falling back
        to legacy 4096; thinking tokens count toward max_tokens and guaranteed truncation
        (same failure mode as #169)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 128_000

    def test_claude_haiku_max_tokens_is_64k(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """haiku-4-5's official output cap is 64K (not 128K) — per-model table, not
        a prefix-shared constant, prevents a small-cap model from borrowing a large cap
        and hitting a server 400."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 64_000

    def test_unregistered_claude_model_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """claude models not registered in the registry (build_model_catalog().models) with a max_output_tokens
        raise immediately — do not fall back to langchain's stale profile (unknown id gives 4096)
        which would borrow the wrong cap."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with pytest.raises(ValueError, match="Unknown claude model"):
            build_chat_model(
                "claude-sonnet-3-9",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_deepseek_empty_effort_skips_extra_body(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Explicit settings.lm.reasoning_effort="" → does not inject output_config, endpoint
        defaults (medium thinking budget). Empty string is an explicit non-None value that
        overrides the per-model "max" from the registry — opt out to a cheaper tier."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs

    def test_deepseek_default_is_max(self, *, model_catalog: ModelCatalog) -> None:
        """DeepSeek's per-model registry default is 'max' — DeepSeek only automatically
        upgrades to max for recognized harnesses (docs note Claude Code / OpenCode), Ava is not
        on that list, so we must explicitly request it. Changing this default will break this
        test, signaling the need to sync docs / runbook."""
        assert (
            resolve_setting(
                "reasoning_effort",
                model="deepseek-flash",
                models=model_catalog.models,
                explicit=get_field("reasoning_effort"),
            )
            == "max"
        )

    def test_deepseek_max_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """effort='max' → injects output_config.effort=max into extra_body, passed through
        by langchain-anthropic to the Anthropic SDK into the POST body."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_high_effort_injects_output_config(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """high is also a valid effort value — DeepSeek docs high/max two tiers;
        explicit value overrides the per-model 'max' from the registry."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "high"}}

    def test_deepseek_none_effort_disables_thinking_instead_of_wire_none(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """effort='none' must never reach `output_config.effort` — DeepSeek's wire
        vocabulary is graded levels only and 400s on it ("unknown variant `none`,
        expected one of high, low, medium, max, xhigh"), which is what took every
        `ava.web.fetch` down (AVA_WEB_FETCH_REASONING ships as "none"). Off is the
        endpoint's thinking switch, which is also what the setting promises."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "disabled"}

    def test_deepseek_none_effort_leaves_caller_thinking_alone(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A caller that passed `thinking` stated its own intent and wins over a
        global effort of 'none' — the effort is dropped rather than overwriting the
        caller's thinking config."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model(
            "deepseek-flash",
            thinking={"type": "enabled", "budget_tokens": 8000},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8000}

    def test_deepseek_rejects_unsupported_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "deepseek-flash",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_shipped_web_fetch_config_builds_an_accepted_request(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """The pair `ava.web.fetch` ships with (AVA_WEB_FETCH_MODEL=deepseek-flash,
        AVA_WEB_FETCH_REASONING=none) has to build a request the endpoint accepts —
        that exact pair is what 400'd on every fetch in production."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model(
            settings.web.web_fetch_model,
            reasoning_effort=settings.web.web_fetch_reasoning,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs
        assert llm.thinking == {"type": "disabled"}

    def test_deepseek_unknown_effort_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A typo'd effort fails fast at build time rather than as a provider 400."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "nonw")
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            build_chat_model(
                "deepseek-flash",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_deepseek_thinking_disabled_skips_extra_body(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """thinking={'type':'disabled'} should not inject output_config.effort even if the
        resolved effort is non-empty — DeepSeek server rejects setting both simultaneously (400
        "thinking options type cannot be disabled when reasoning_effort is set"). The labeler
        short-text path explicitly disables thinking; the global env effort must not sneak back in."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model(
            "deepseek-flash",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert "extra_body" not in llm.model_kwargs

    def test_deepseek_thinking_enabled_still_injects_extra_body(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """thinking={'type':'enabled', ...} does not conflict — the server accepts thinking enabled
        together with reasoning_effort. Only thinking=disabled is mutually exclusive with effort."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model(
            "deepseek-flash",
            thinking={"type": "enabled", "budget_tokens": 8000},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_wins(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Explicit reasoning_effort parameter overrides the resolved effort —
        allowing a caller like syntax repair to lock on max without being dragged down
        by a global config set to a lower effort by some agent."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model(
            "deepseek-flash",
            reasoning_effort="max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_reasoning_effort_override_when_global_empty(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """When global effort is empty, explicit override still injects — the override is an
        independent source, not dependent on the global being non-empty."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model(
            "deepseek-flash",
            reasoning_effort="max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.model_kwargs["extra_body"] == {"output_config": {"effort": "max"}}

    def test_deepseek_missing_api_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Missing DEEPSEEK_API_KEY raises RuntimeError fail-fast, rather than silently falling
        back to ANTHROPIC_API_KEY and only discovering the issue through a 401."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            build_chat_model(
                "deepseek-flash",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_claude_missing_api_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """Missing ANTHROPIC_API_KEY raises RuntimeError fail-fast — consistent with all other
        provider branches. Previously claude-* lacked this check; ChatAnthropic with no key
        silently hung, the agent process stuck in the LLM call never returning."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            build_chat_model(
                "claude-opus-5",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_gemini_branch_builds(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        m = build_chat_model(
            "gemini-3.1-pro-preview",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        from langchain_google_genai import ChatGoogleGenerativeAI

        assert isinstance(m, ChatGoogleGenerativeAI)

    def test_gemini_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            build_chat_model(
                "gemini-3.1-pro-preview",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )

    def test_gemini_enables_include_thoughts(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """gemini-* must set include_thoughts=True — otherwise the model still thinks but returns
        zero thought blocks (live view zero reasoning). When enabled, thoughts are returned as
        `{"type":"thinking","thinking":...}` content blocks, same shape as claude/deepseek,
        reusing the existing streaming/timeline path without a provider branch."""
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
        assert m.include_thoughts is True

    def test_gemini_thinking_disabled_drops_include_thoughts(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """thinking={'type':'disabled'} (short-text path) → include_thoughts=False,
        no thought blocks returned. Symmetric with deepseek thinking-disabled skipping effort
        injection: the caller explicitly disables reasoning, so thinking should not be emitted."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.include_thoughts is False

    def test_gemini_media_args_build(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
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
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.media_resolution == MediaResolution.MEDIA_RESOLUTION_HIGH
        assert m.thinking_level == "low"
        assert m.base_url == "http://localhost:8080/v1beta"  # type: ignore[reportUnknownMemberType]
        assert m.include_thoughts is None

    def test_gemini_media_resolution_maps_each_level(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from google.genai.types import MediaResolution
        from langchain_google_genai import ChatGoogleGenerativeAI

        for setting, enum in [
            ("low", MediaResolution.MEDIA_RESOLUTION_LOW),
            ("medium", MediaResolution.MEDIA_RESOLUTION_MEDIUM),
            ("high", MediaResolution.MEDIA_RESOLUTION_HIGH),
        ]:
            m = build_chat_model(
                "gemini-3.5-flash",
                media_resolution=setting,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )
            assert isinstance(m, ChatGoogleGenerativeAI)
            assert m.media_resolution == enum

    def test_gemini_invalid_media_resolution_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        with pytest.raises(ValueError, match="media_resolution"):
            build_chat_model(
                "gemini-3.5-flash",
                media_resolution="ultra",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
                ),
            )
