from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from base.config import settings
from base.host.env import dotenv_file
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model, validate_model_config
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import AddModels


class TestValidateModelConfig:
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
            dotenv_file,
            "read_env_aliases",
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

    def test_model_from_config_wins_over_default(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="claude-sonnet-5",
            config={"llm_model": "deepseek-flash"},
            catalog=model_catalog,
            llm_override="",
        )
        assert result == "deepseek-flash"

    def test_fallback_to_cluster_default_when_config_omits_model(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash",
            config={"some_other_key": "value"},
            catalog=model_catalog,
            llm_override="",
        )
        assert result == "deepseek-flash"

    def test_fallback_to_cluster_default_when_config_is_none(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash", config=None, catalog=model_catalog, llm_override=""
        )
        assert result == "deepseek-flash"

    def test_no_model_configured_raises(self, model_catalog: ModelCatalog) -> None:
        with pytest.raises(ValueError, match="no model configured"):
            validate_model_config(model=None, config=None, catalog=model_catalog, llm_override="")

    def test_no_model_configured_empty_config(self, model_catalog: ModelCatalog) -> None:
        with pytest.raises(ValueError, match="no model configured"):
            validate_model_config(model=None, config={}, catalog=model_catalog, llm_override="")

    def test_unknown_model_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="unknown model 'not-a-real-model'"):
            validate_model_config(model="not-a-real-model", catalog=model_catalog, llm_override="")

    def test_known_model_succeeds(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash", catalog=model_catalog, llm_override=""
        )
        assert result == "deepseek-flash"

    def test_all_supported_models_pass_name_check(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_plugin_keys(monkeypatch)
        all_models = [m for models in model_catalog.supported_models.values() for m in models]
        for m in all_models:
            self._set_plugin_keys(monkeypatch)
            result = validate_model_config(model=m, catalog=model_catalog, llm_override="")
            assert result == m

    def test_superseded_model_stays_spawn_valid(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch, add_models: AddModels
    ) -> None:
        from dataclasses import replace

        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        model_catalog = add_models(
            model_catalog,
            {"glm-5.2": replace(model_catalog.models["glm-5.2"], superseded_by="kimi-k3")},
        )
        result = validate_model_config(
            model="glm-5.2", config={"llm_model": "glm-5.2"}, catalog=model_catalog, llm_override=""
        )
        assert result == "glm-5.2"

    def test_config_only_validation_keeps_exact_model_effort_checks(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        assert (
            validate_model_config(
                config={"llm_model": "deepseek-flash", "reasoning_effort": "max"},
                check_provider_key=False,
                catalog=model_catalog,
                llm_override="",
            )
            == "deepseek-flash"
        )
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            validate_model_config(
                config={"llm_model": "deepseek-flash", "reasoning_effort": "low"},
                check_provider_key=False,
                catalog=model_catalog,
                llm_override="",
            )
        with pytest.raises(ValueError, match="unknown model"):
            validate_model_config(
                model="unknown-model",
                check_provider_key=False,
                catalog=model_catalog,
                llm_override="",
            )

    def test_missing_claude_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(model="claude-sonnet-5", catalog=model_catalog, llm_override="")

    def test_missing_deepseek_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            validate_model_config(model="deepseek-flash", catalog=model_catalog, llm_override="")

    def test_missing_gemini_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            validate_model_config(model="gemini-3.5-flash", catalog=model_catalog, llm_override="")

    def test_missing_openai_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            validate_model_config(model="gpt-5.6-sol", catalog=model_catalog, llm_override="")

    def test_missing_mimo_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "xiaomi_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="MIMO_API_KEY"):
            validate_model_config(model="mimo-v2.5-pro", catalog=model_catalog, llm_override="")

    def test_missing_kimi_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(settings.lm, "moonshot_api_key", SecretStr("legacy-settings-key"))
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
            validate_model_config(model="kimi-k3", catalog=model_catalog, llm_override="")

    def test_missing_glm_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="GLM_API_KEY"):
            validate_model_config(model="glm-5.2", catalog=model_catalog, llm_override="")

    def test_missing_qwen_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
        with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
            validate_model_config(model="qwen3.8-max", catalog=model_catalog, llm_override="")

    def test_key_present_succeeds(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            dotenv_file,
            "read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(
            model="claude-sonnet-5", catalog=model_catalog, llm_override=""
        )
        assert result == "claude-sonnet-5"

    def test_config_model_with_missing_key_raises(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            dotenv_file,
            "read_env_aliases",
            lambda: {
                "DEEPSEEK_API_KEY": "sk-test",
                "GEMINI_API_KEY": "sk-test",
            },
        )
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            validate_model_config(
                model="deepseek-flash",
                config={"llm_model": "claude-sonnet-5"},
                catalog=model_catalog,
                llm_override="",
            )

    def test_config_model_with_key_present_succeeds(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        monkeypatch.setattr(
            dotenv_file,
            "read_env_aliases",
            lambda: {"ANTHROPIC_API_KEY": "sk-ant-123"},
        )
        result = validate_model_config(
            model="deepseek-flash",
            config={"llm_model": "claude-sonnet-5"},
            catalog=model_catalog,
            llm_override="",
        )
        assert result == "claude-sonnet-5"

    def test_config_with_non_string_model_ignores(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)
        result = validate_model_config(
            model="deepseek-flash",
            config={"llm_model": 42},
            catalog=model_catalog,
            llm_override="",
        )
        assert result == "deepseek-flash"

    def test_override_skips_api_key_check(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        llm_override = "tests.fakes:build"
        result = validate_model_config(
            model="claude-sonnet-5", catalog=model_catalog, llm_override=llm_override
        )
        assert result == "claude-sonnet-5"

    @pytest.mark.parametrize(
        ("model", "effort"),
        [("mimo-v2.6-pro", "none"), ("mimo-v2.6-pro", "high"), ("deepseek-flash", "max")],
    )
    def test_supported_explicit_effort_passes_unchanged(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch, model: str, effort: str
    ) -> None:
        self._set_plugin_keys(monkeypatch)
        config: dict[str, object] = {"reasoning_effort": effort}
        assert (
            validate_model_config(
                model=model, config=config, catalog=model_catalog, llm_override=""
            )
            == model
        )
        assert config["reasoning_effort"] == effort

    @pytest.mark.parametrize(
        ("model", "effort", "message"),
        [
            ("mimo-v2.6-pro", "low", "unsupported reasoning effort"),
            ("deepseek-flash", "medium", "unsupported reasoning effort"),
            ("gpt-6.1-sol-fast", "none", "unsupported reasoning effort"),
            ("deepseek-flash", "invented", "unknown reasoning effort"),
            ("deepseek-flash", 42, "reasoning_effort must be a string"),
        ],
    )
    def test_invalid_explicit_effort_rejected_even_with_override(
        self,
        model_catalog: ModelCatalog,
        monkeypatch: pytest.MonkeyPatch,
        model: str,
        effort: object,
        message: str,
    ) -> None:
        llm_override = "tests.fakes:build"
        with pytest.raises(ValueError, match=message):
            validate_model_config(
                model=model,
                config={"reasoning_effort": effort},
                catalog=model_catalog,
                llm_override=llm_override,
            )

    def test_restored_model_validates_to_itself(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_all_keys(monkeypatch)
        self._set_plugin_keys(monkeypatch)

        result = validate_model_config(
            model="gemini-3.8-flash", catalog=model_catalog, llm_override=""
        )

        assert result == "gemini-3.8-flash"


class TestThinkingDisabledAcrossRoster:
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
        "model", [m for models in build_model_catalog().supported_models.values() for m in models]
    )
    def test_roster_model_constructs_with_thinking_disabled(
        self, model_catalog: ModelCatalog, model: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_all_keys(monkeypatch)
        llm = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=ModelOverrides.from_pins({}),
        )
        assert isinstance(llm, BaseChatModel)

    def test_unregistered_gemini_thinking_disabled_is_noop(
        self,
        model_catalog: ModelCatalog,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        model = "gemini-2.5-flash"
        assert model not in model_catalog.models
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model(
            model,
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=ModelOverrides.from_pins({}),
        )
        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.thinking_level is None
        assert llm.include_thoughts is None
        assert any(model in r["message"] and "ignored" in r["message"] for r in loguru_records)

    def test_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level(
        self, model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = build_chat_model(
            "gemini-3.1-pro-preview",
            thinking={"type": "disabled"},
            catalog=model_catalog,
            llm_override="",
            overrides=ModelOverrides.from_pins({}),
        )
        assert isinstance(llm, ChatGoogleGenerativeAI)
        assert llm.thinking_level == "low"
        assert llm.include_thoughts is False
