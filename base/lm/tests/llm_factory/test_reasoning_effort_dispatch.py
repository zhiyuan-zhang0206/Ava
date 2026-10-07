"""Llm factory cases: reasoning effort dispatch."""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic

from base.config import settings
from base.lm.factory import _resolve_override, build_chat_model
from base.lm.tests.test_llm_factory import _FakeLLM, _install_fake_module


class TestReasoningEffortDispatch:
    """Per-provider injection / clamping / gating tests for AVA_REASONING_EFFORT.

    Across provider vocabularies none/minimal/low/medium/high/xhigh/max, clamp_effort
    maps to what each provider truly accepts: out-of-range values clamp to the nearest
    tier (ties round up, matching the precedent where DeepSeek server maps low/medium→high,
    xhigh→max); misspelled values explode at build time instead of landing as a provider 400.
    """

    def test_clamp_unknown_value_raises(self) -> None:
        from base.lm.factory import clamp_effort

        with pytest.raises(ValueError, match="unknown reasoning effort"):
            clamp_effort("higth", ("low", "high"), target="test")

    def test_clamp_ties_round_up(self) -> None:
        """medium is equidistant from low/high → picks high; xhigh is equidistant from
        high/max → picks max."""
        from base.lm.factory import clamp_effort

        assert clamp_effort("medium", ("low", "high", "max"), target="test") == "high"
        assert clamp_effort("xhigh", ("low", "high", "max"), target="test") == "max"
        assert clamp_effort("none", ("low", "medium", "high"), target="test") == "low"

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
            "claude-opus-5-5",
            "claude-fable-5",
            "claude-fable-5-1",
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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("glm-5.2")
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "high"

    def test_glm_5_3_low_effort_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GLM-5.3 documents low as a native reasoning-effort rung."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model("glm-5.2", thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
        assert m.reasoning_effort is None

    @pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"])
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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

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
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

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

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """MiMo officially documents thinking.type enabled/disabled — disabled goes through the
        body top-level thinking (previously silently swallowed, F5). effort is not mentioned
        in the official reference, so it is not connected."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_high_effort_is_noop(self, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
        """MiMo has no graded reasoning_effort field — 'max' clamps to the
        two-value ('none', 'high') table's 'high' tier, which is the provider
        default (thinking already on) and needs no extra_body at all."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "max")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model)
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_none_effort_disables_thinking_body(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """AVA_REASONING_EFFORT='none' clamps to the table's 'none' tier — the
        only tier that differs from the provider default — and maps onto the
        same body thinking.type=disabled switch as an explicit thinking arg."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "none")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model)
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_low_effort_clamps_to_high(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """low sits equidistant from none/high in the cross-provider vocab —
        clamp ties round up, so it lands on 'high' (provider default, no-op),
        not 'none' (which would silently disable thinking)."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model)
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_explicit_thinking_disabled_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """Caller-explicit thinking={'type':'disabled'} (short-text paths) wins
        outright — reasoning_effort is not even consulted."""
        monkeypatch.setattr(settings.lm, "llm_override", "")
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr(settings.lm, "reasoning_effort", "high")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}


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
