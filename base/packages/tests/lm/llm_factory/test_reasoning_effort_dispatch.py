from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic

from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model


class TestReasoningEffortDispatch:
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

    def test_claude_effort_injected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="xhigh"),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort == "xhigh"

    def test_claude_empty_effort_not_injected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort=""),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_haiku_effort_field_ignored(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_claude_thinking_disabled_skips_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="max"),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.effort is None

    def test_haiku_thinking_budget_opts_in(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=8192),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}

    def test_haiku_budget_zero_leaves_thinking_off(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=0),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_thinking_wins_over_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=8192),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    def test_haiku_high_effort_opts_in_at_default_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=0, reasoning_effort="high"),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 8192}
        assert llm.effort is None

    def test_haiku_none_effort_leaves_thinking_off(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=0, reasoning_effort="none"),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking is None

    def test_haiku_explicit_budget_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-haiku-4-5-20251001",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(
                claude_thinking_budget_tokens=20_000, reasoning_effort="high"
            ),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "enabled", "budget_tokens": 20_000}

    def test_sonnet_ignores_thinking_budget(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(claude_thinking_budget_tokens=8192),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_claude_defaults_to_summarized_display(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        # meiva:ref base/packages/tests/lm/docs/contracts.meiva.okf.md#reasoning-visibility
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
                llm_override="",
                overrides=model_overrides(),
            )
            assert isinstance(llm, ChatAnthropic)
            assert llm.thinking == {"type": "adaptive", "display": "summarized"}, model

    def test_adaptive_caller_thinking_gets_display_filled(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "adaptive"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "summarized"}

    def test_adaptive_caller_display_respected(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        # meiva:ref base/packages/tests/lm/docs/contracts.meiva.okf.md#reasoning-visibility
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "adaptive", "display": "omitted"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "adaptive", "display": "omitted"}

    def test_claude_thinking_disabled_no_adaptive_injection(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = build_chat_model(
            "claude-sonnet-5",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.thinking == {"type": "disabled"}

    def test_gemini_effort_maps_to_thinking_level(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="low"),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "low"

    def test_gemini_rejects_max(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "gemini-3.5-flash",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="max"),
            )

    def test_gemini_default_leaves_thinking_level_unset(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort=""),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level is None

    def test_gemini_thinking_disabled_drops_to_minimal(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.thinking_level == "minimal"
        assert m.include_thoughts is False

    def test_kimi_effort_injected_via_extra_body(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="low"),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.extra_body == {"reasoning_effort": "low"}

    def test_kimi_rejects_medium(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "kimi-k3",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="medium"),
            )

    def test_kimi_thinking_disabled_sends_no_thinking_param(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort=""),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.thinking is None
        assert m.extra_body is None

    def test_glm_effort_injected_as_field(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "high"

    def test_glm_5_3_low_effort_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.3",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="low"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.reasoning_effort == "low"

    def test_glm_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
        assert m.reasoning_effort is None

    @pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"])
    def test_glm_5_3_thinking_disabled_warns_instead_of_sending_body(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body is None
        assert m.reasoning_effort is None

    def test_qwen_none_effort_disables_thinking(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="none"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}
        assert m.reasoning_effort is None

    def test_qwen_rejects_graded_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="low"),
            )

    def test_qwen_thinking_disabled_sends_enable_thinking_false(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"enable_thinking": False}

    def test_qwen_unknown_effort_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        with pytest.raises(ValueError, match="unknown reasoning effort"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="hihg"),
            )

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_thinking_disabled_sends_body_thinking(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
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
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
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
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="none"),
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
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                model,
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="low"),
            )

    @pytest.mark.parametrize(
        "model",
        ("mimo-v2.5-pro", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"),
    )
    def test_mimo_explicit_thinking_disabled_wins_over_effort(
        self, monkeypatch: pytest.MonkeyPatch, model: str, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(reasoning_effort="high"),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.extra_body == {"thinking": {"type": "disabled"}}
