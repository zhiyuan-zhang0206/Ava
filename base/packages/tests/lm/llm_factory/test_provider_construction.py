from __future__ import annotations

import os

import pytest
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from base.config import settings
from base.config.tests.configuration import model_overrides
from base.host.env import dotenv_file
from base.lm.catalog import ModelCatalog
from base.lm.factory import (
    build_chat_model,
)


class TestProviderConstruction:
    def test_gpt_branch_builds(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        from langchain_openai import ChatOpenAI

        assert isinstance(m, ChatOpenAI)

    def test_gpt_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "openai_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            build_chat_model(
                "gpt-5.6-sol",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_gpt_enables_responses_api_reasoning_summary(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.use_responses_api is True
        assert m.reasoning == {"effort": "medium", "summary": "auto"}

    def test_gpt_rejects_unsupported_effort(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            build_chat_model(
                "gpt-5.6-sol",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(reasoning_effort="minimal"),
            )

    def test_gpt_thinking_disabled_drops_to_effort_none(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
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
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "xiaomimimo.com" in str(m.openai_api_base)

    def test_mimo_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MIMO_API_KEY"):
            build_chat_model(
                "mimo-v2.5-pro",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_kimi_returns_chat_moonshot(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatMoonshot)

    def test_kimi_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            build_chat_model(
                "kimi-k3",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_unknown_prefix_raises(self, *, model_catalog: ModelCatalog) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            build_chat_model(
                "llama-3",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_unknown_prefix_error_hints_at_adding_branch(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        with pytest.raises(ValueError) as exc_info:
            build_chat_model(
                "mistral-large",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )
        assert "mistral-*" in str(exc_info.value)

    def test_glm_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_5_3_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-glm")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.3-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert "bigmodel.cn" in str(m.openai_api_base)

    def test_glm_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "zhipu_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GLM_API_KEY"):
            build_chat_model(
                "glm-5.2",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_qwen_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_qwen3_8_flash_returns_reasoning_content_model(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert m.stream_usage is True

    def test_qwen_honors_the_configured_base_url(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        workspace = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        monkeypatch.setattr(settings.lm, "dashscope_base_url", workspace)
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert str(m.openai_api_base) == workspace

    def test_qwen_requests_stream_usage(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.stream_usage is True

    def test_qwen_missing_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setattr(settings.lm, "dashscope_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            build_chat_model(
                "qwen3.8-max",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_kimi_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False

    def test_kimi_k2_7_code_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k2.7-code",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
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
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            model,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_glm_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GLM_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "glm-5.2",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_qwen_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        from base.lm.compat.openai_reasoning import ReasoningContentChatModel

        m = build_chat_model(
            "qwen3.8-max",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ReasoningContentChatModel)
        assert m.disable_streaming is False

    def test_claude_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        m = build_chat_model(
            "claude-sonnet-5",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_deepseek_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        m = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatAnthropic)
        assert m.disable_streaming is False

    def test_deepseek_gateway_popped_key_falls_back_to_env_file(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(
            dotenv_file,
            "read_env_aliases",
            lambda: {"DEEPSEEK_API_KEY": "sk-file-value"},
        )

        m = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )

        assert isinstance(m, ChatAnthropic)
        assert m.anthropic_api_key.get_secret_value() == "sk-file-value"
        assert "DEEPSEEK_API_KEY" not in os.environ

    def test_deepseek_env_var_still_wins_over_env_file(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-value")
        monkeypatch.setattr(
            dotenv_file,
            "read_env_aliases",
            lambda: {"DEEPSEEK_API_KEY": "sk-file-value"},
        )

        m = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )

        assert isinstance(m, ChatAnthropic)
        assert m.anthropic_api_key.get_secret_value() == "sk-env-value"

    def test_deepseek_missing_key_in_both_channels_still_raises(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)

        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            build_chat_model(
                "deepseek-flash",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

    def test_gemini_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is False

    def test_gpt_defaults_to_streaming(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        from langchain_openai import ChatOpenAI

        m = build_chat_model(
            "gpt-5.6-sol",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatOpenAI)
        assert m.disable_streaming is False

    def test_explicit_streaming_false_on_non_kimi(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from langchain_google_genai import ChatGoogleGenerativeAI

        m = build_chat_model(
            "gemini-3.5-flash",
            streaming=False,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatGoogleGenerativeAI)
        assert m.disable_streaming is True

    def test_explicit_streaming_true_overrides_kimi_default(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
        from langchain_moonshot import ChatMoonshot

        m = build_chat_model(
            "kimi-k3",
            streaming=True,
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(m, ChatMoonshot)
        assert m.disable_streaming is False
