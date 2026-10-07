"""Llm factory cases: validate model config."""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from base.config import settings
from base.lm.factory import build_chat_model, validate_model_config
from base.lm.plugin_providers import model_catalog
from tests.fixtures.model_catalog import AddModels


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
            "base.host.env.runtime_config.read_env_aliases",
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
            config={"llm_model": "deepseek-flash"},
        )
        assert result == "deepseek-flash"

    def test_fallback_to_cluster_default_when_config_omits_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config doesn't have llm_model, use the cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash",
            config={"some_other_key": "value"},
        )
        assert result == "deepseek-flash"

    def test_fallback_to_cluster_default_when_config_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config=None, use the cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(model="deepseek-flash", config=None)
        assert result == "deepseek-flash"

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
        """model name not in model_catalog().supported_models → ValueError."""
        with pytest.raises(ValueError, match="unknown model 'not-a-real-model'"):
            validate_model_config(model="not-a-real-model")

    def test_known_model_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registered model → returns model name."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(model="deepseek-flash")
        assert result == "deepseek-flash"

    def test_all_supported_models_pass_name_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every model in model_catalog().supported_models passes the name check."""
        self._set_plugin_keys(monkeypatch)
        all_models = [m for models in model_catalog().supported_models.values() for m in models]
        for m in all_models:
            # Only testing name existence, not key (key validation is separate)
            self._set_plugin_keys(monkeypatch)
            result = validate_model_config(model=m)
            assert result == m

    def test_superseded_model_stays_spawn_valid(
        self, monkeypatch: pytest.MonkeyPatch, add_models: AddModels
    ) -> None:
        """Supersession is display-only: a registry model carrying
        ``superseded_by`` (hidden from the picker) must keep passing spawn
        validation — settings/config switching back to it stays allowed."""
        from dataclasses import replace

        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        add_models({"glm-5.2": replace(model_catalog().models["glm-5.2"], superseded_by="kimi-k3")})
        result = validate_model_config(model="glm-5.2", config={"llm_model": "glm-5.2"})
        assert result == "glm-5.2"

    # --- API key validation -----------------------------------------------------

    def test_missing_claude_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(model="claude-sonnet-5")

    def test_missing_deepseek_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEEPSEEK_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            validate_model_config(model="deepseek-flash")

    def test_missing_gemini_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GEMINI_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            validate_model_config(model="gemini-3.5-flash")

    def test_missing_openai_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            validate_model_config(model="gpt-5.6-sol")

    def test_missing_mimo_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MIMO_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="MIMO_API_KEY"):
            validate_model_config(model="mimo-v2.5-pro")

    def test_missing_kimi_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MOONSHOT_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
            validate_model_config(model="kimi-k3")

    def test_missing_glm_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GLM_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="GLM_API_KEY"):
            validate_model_config(model="glm-5.2")

    def test_missing_qwen_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DASHSCOPE_API_KEY not set → ValueError."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
        with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
            validate_model_config(model="qwen3.8-max")

    def test_key_present_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """key is set → validation passes."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "base.host.env.runtime_config.read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(model="claude-sonnet-5")
        assert result == "claude-sonnet-5"

    def test_config_model_with_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model points to a provider with missing key → ValueError (not the cluster default's key)."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "base.host.env.runtime_config.read_env_aliases",
            lambda: {
                "DEEPSEEK_API_KEY": "sk-test",
                "GEMINI_API_KEY": "sk-test",
            },
        )
        # The cluster default has its plugin key, but config picks claude → fail.
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(
                model="deepseek-flash",  # cluster default
                config={"llm_model": "claude-sonnet-5"},  # per-agent override
            )

    def test_config_model_with_key_present_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model's provider key is set → passes. The cluster default is irrelevant."""
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            "base.host.env.runtime_config.read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(
            model="deepseek-flash",  # cluster default (has no deepseek key)
            config={"llm_model": "claude-sonnet-5"},  # per-agent (has key)
        )
        assert result == "claude-sonnet-5"

    def test_config_with_non_string_model_ignores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.llm_model is not a str → ignored, fallback to cluster default."""
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash",
            config={"llm_model": 42},  # not a string
        )
        assert result == "deepseek-flash"

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

    @pytest.mark.parametrize(
        "model", [m for models in model_catalog().supported_models.values() for m in models]
    )
    def test_roster_model_constructs_with_thinking_disabled(
        self, model: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every spawnable model can be constructed with thinking disabled — a
        provider that cannot express it must no-op, not 400."""
        self._stub_all_keys(monkeypatch)
        llm = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(llm, BaseChatModel)

    def test_unregistered_gemini_thinking_disabled_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
    ) -> None:
        """An intentionally unregistered Gemini id has no thinking_level vocabulary:
        disabled must send no thinking parameters and warn (issue #190)."""
        model = "gemini-2.5-flash"
        assert model not in model_catalog().models
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model(model, thinking={"type": "disabled"})
        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.thinking_level is None
        assert llm.include_thoughts is None
        assert any(model in r["message"] and "ignored" in r["message"] for r in loguru_records)

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
