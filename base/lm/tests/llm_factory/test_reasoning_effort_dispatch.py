"""Llm factory cases: reasoning effort dispatch."""

from __future__ import annotations

from dataclasses import fields

import pytest
from langchain_anthropic import ChatAnthropic

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model


class TestReasoningEffortDispatch:
    """Per-provider injection / validation / gating tests for AVA_REASONING_EFFORT.

    Model-specific options pass through exactly; unsupported effort values fail
    before dispatch instead of being changed to a different tier.
    """

    def test_validation_rejects_unknown_effort(self) -> None:
        from base.lm.factory import validate_effort

        with pytest.raises(ValueError, match="unknown reasoning effort"):
            validate_effort("higth", ("low", "high"), target="test")

    def test_validation_never_remaps_a_known_effort(self) -> None:
        from base.lm.factory import validate_effort

        for effort, levels in [
            ("medium", ("low", "high", "max")),
            ("xhigh", ("low", "high", "max")),
            ("none", ("low", "medium", "high")),
        ]:
            with pytest.raises(ValueError, match="unsupported reasoning effort"):
                validate_effort(effort, levels, target="test")

    # ── claude ──────────────────────────────────────────────────────────

    def test_claude_effort_injected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """sonnet-5 supports effort → uses ChatAnthropic's effort field
        (on the wire it is output_config.effort)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "xhigh")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == "xhigh"

    def test_claude_empty_effort_not_injected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_haiku_effort_field_ignored(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """haiku-4-5 does not support ChatAnthropic's `effort` field (server 400) — that
        knob is ignored rather than passed through and causing an error. AVA_REASONING_EFFORT
        itself is not completely ignored — it instead maps to the thinking budget
        mapping (see TestReasoningEffortDispatch's test_haiku_high_effort_opts_in_at_default_budget)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_thinking_disabled_skips_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """caller explicitly disables thinking (labeler/judge short-text path) → do not
        inject effort, aligning with the deepseek branch: the global env should not sneak
        reasoning back in."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_haiku_thinking_budget_opts_in(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """haiku-4-5 defaults thinking OFF; when AVA_CLAUDE_THINKING_BUDGET_TOKENS>0,
        injects thinking={'type':'enabled','budget_tokens':N} so the haiku agent really thinks."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}

    def test_haiku_budget_zero_leaves_thinking_off(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_thinking_wins_over_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """caller explicitly passes thinking (e.g. labeler's disabled) always overrides
        the budget config."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    def test_haiku_high_effort_opts_in_at_default_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """haiku-4-5 has no `effort` field (server 400) but AVA_REASONING_EFFORT
        still does something: validated against the model's ('none','high') binary,
        'high' opts extended thinking in at the fallback default budget — this
        is the only lever available when claude_thinking_budget_tokens is unset,
        so the spawn-UI effort dropdown isn't inert for this model."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}
        assert llm.effort is None

    def test_haiku_none_effort_leaves_thinking_off(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 0)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_budget_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """settings.lm.claude_thinking_budget_tokens (an explicit numeric
        budget) always wins over the effort-derived default — an operator who
        tuned the budget directly shouldn't have a spawn-time effort pick
        silently override it to the generic 8192 fallback."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 20_000)
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 20_000}

    def test_sonnet_ignores_thinking_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """On adaptive-thinking models (sonnet-5), enabled+budget_tokens is a server
        400 — budget config only acts on extended-thinking-only models (haiku).
        Ignoring the budget does not leave thinking unset: the branch still sends the
        adaptive default with display='summarized'."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(settings.lm, "claude_thinking_budget_tokens", 8192)
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_claude_defaults_to_summarized_display(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
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
            "claude-opus-5-5",
            "claude-fable-5",
            "claude-fable-5-1",
        ):
            llm = build_chat_model(
                model,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )
            assert isinstance(llm, ChatAnthropic)
            assert llm.thinking == {"type": "adaptive", "display": "summarized"}, model

    def test_adaptive_caller_thinking_gets_display_filled(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A caller-passed adaptive config without display gets summarized filled in."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "adaptive"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_caller_display_respected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A caller that explicitly chose display='omitted' keeps it — the factory
        only fills the field when absent."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "adaptive", "display": "omitted"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "omitted"}

    def test_claude_thinking_disabled_no_adaptive_injection(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """thinking={'type':'disabled'} (labeler/judge short-text path) passes through
        untouched — no adaptive default, no display added."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    # ── gemini ──────────────────────────────────────────────────────────

    def test_gemini_effort_maps_to_thinking_level(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "low"

    def test_gemini_rejects_max(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "gemini-3.5-flash",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    def test_gemini_default_leaves_thinking_level_unset(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """effort empty → thinking_level=None → model default tier (Flash medium / Pro high)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level is None

    def test_gemini_thinking_disabled_drops_to_minimal(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """The semantics of disabled changed from "turn off visibility" to "truly lower the tier":
        only disabling include_thoughts still causes the model to think and bill, so we must also
        set thinking_level='minimal' to be a cost switch."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "minimal"
        assert m.include_thoughts is False

    # ── kimi ────────────────────────────────────────────────────────────

    def test_kimi_effort_injected_via_extra_body(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """kimi-k3 defaults max (most expensive) and is non-streaming — effort is the
        only downgrade knob, via the top-level reasoning_effort body field (extra_body channel)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.extra_body == {"reasoning_effort": "low"}

    def test_kimi_rejects_medium(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "medium")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "kimi-k3",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    def test_kimi_thinking_disabled_sends_no_thinking_param(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """K3 cannot disable thinking and does not accept the K2.x thinking parameter —
        a disabled request logs a warning and ignores, passing nothing (passing would 400)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.thinking is None
        assert m.extra_body is None

    # ── glm ─────────────────────────────────────────────────────────────

    def test_glm_effort_injected_as_field(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """glm-5.2 defaults max — reasoning_effort is an OpenAI standard payload field,
        directly using ChatOpenAI's declared field (stuffing into model_kwargs is rejected
        by langchain)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "high"

    def test_glm_5_3_low_effort_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """GLM-5.3 documents low as a native reasoning-effort rung."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.3",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "low"

    def test_glm_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """GLM natively supports disabling thinking (body top-level thinking.type=disabled) —
        previously silently swallowed (F5). disabled also skips effort injection (caller wants
        the money-saving path)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
        assert m.reasoning_effort is None

    @pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"])
    def test_glm_5_3_thinking_disabled_warns_instead_of_sending_body(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """GLM-5.3 / GLM-5.3-Flash always think: the endpoint rejects
        thinking.type=disabled with a 400 (error code 1210, live-checked
        2026-08-27), so the builder drops the disabled body and warns (the kimi
        branch's pattern) instead of sending a body that fails every call."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    # ── qwen ────────────────────────────────────────────────────────────

    def test_qwen_none_effort_disables_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """DashScope's compatible-mode endpoint has no graded effort field — its
        graded knob is a token budget (`thinking_budget`) and the OpenAI-standard
        `reasoning_effort` string is documented only for its Responses API, which
        Ava does not bind. So the cross-provider knob uses the binary
        none/high and 'none' lands on the endpoint's own off-switch."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}
        # never the OpenAI-standard field: this endpoint would ignore or 400 it
        assert m.reasoning_effort is None

    def test_qwen_rejects_graded_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    def test_qwen_thinking_disabled_sends_enable_thinking_false(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A caller disabling thinking (the labeler / judge short-text path) wins
        over a global effort that would otherwise leave reasoning on."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}

    def test_qwen_unknown_effort_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """A typo'd effort fails fast at build time, not as a provider 400."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "hihg")
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    # ── mimo ────────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """MiMo officially documents thinking.type enabled/disabled — disabled goes through the
        body top-level thinking (previously silently swallowed, F5). effort is not mentioned
        in the official reference, so it is not connected."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_high_effort_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """MiMo has no graded reasoning_effort field — 'high' selects the
        two-value ('none', 'high') table's 'high' tier, which is the provider
        default (thinking already on) and needs no extra_body at all."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_none_effort_disables_thinking_body(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """AVA_REASONING_EFFORT='none' selects the declared 'none' tier — the
        only tier that differs from the provider default — and maps onto the
        same body thinking.type=disabled switch as an explicit thinking arg."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_rejects_low_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                model,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_explicit_thinking_disabled_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        """Caller-explicit thinking={'type':'disabled'} (short-text paths) wins
        outright — reasoning_effort is not even consulted."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
