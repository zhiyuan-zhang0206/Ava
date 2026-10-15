from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic

from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model


class TestDeepseekMaxTokens:
    def test_flash_max_tokens(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        llm = build_chat_model(
            "deepseek-flash",
            catalog=model_catalog,
            llm_override="",
            overrides=model_overrides(),
        )
        assert isinstance(llm, ChatAnthropic)
        assert llm.max_tokens == 384_000

    def test_unknown_deepseek_model_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
        with pytest.raises(ValueError, match="Unknown deepseek model") as exc_info:
            build_chat_model(
                "deepseek-v4-nonexistent",
                catalog=model_catalog,
                llm_override="",
                overrides=model_overrides(),
            )

        message = str(exc_info.value)
        assert "Known deepseek models:" in message
        assert "deepseek-flash" in message
        assert "deepseek-v4-pro" not in message


class TestModelContextWindow:
    def test_deepseek_flash_is_one_million(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["deepseek-flash"] == 1_000_000

    def test_kimi_k3_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["kimi-k3"] == 1_048_576

    def test_glm_5_2_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["glm-5.2"] == 1_000_000

    def test_glm_5_3_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["glm-5.3"] == 1_000_000

    def test_glm_5_3_flash_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["glm-5.3-flash"] == 1_000_000

    def test_glm_5_3_flashx_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["glm-5.3-flashx"] == 1_000_000

    def test_mimo_v2_6_pro_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["mimo-v2.6-pro"] == 1_000_000

    def test_mimo_v2_6_pro_ultraspeed_is_1m(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.context_windows["mimo-v2.6-pro-ultraspeed"] == 1_000_000

    def test_qwen3_8_flash_window_is_thinking_mode_input(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        assert model_catalog.context_windows["qwen3.8-flash"] == 983_616


class TestModelIdentity:
    def test_kimi_k3_identity(self, *, model_catalog: ModelCatalog) -> None:
        assert model_catalog.identities["kimi-k3"] == "You are running on Kimi K3 (Moonshot)."
