"""Plugin provider key validation and model media capability tests.

Anthropic, DeepSeek, Gemini, OpenAI, Qwen, and Zhipu are provider plugins, so
their key declarations are plugin-owned and the cluster's `.env` file is the
only spawn-validation source. The legacy Settings fields stay for
configuration compatibility but no longer authorize a spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import settings
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.factory import model_supports_vision, provider_key_map, validate_model_config


@pytest.fixture
def env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the plugin key reader at a scratch cluster `.env`."""
    import shared.runtime_config as rc

    env_path = tmp_path / ".env"
    monkeypatch.setattr(rc, "env_file_path", lambda: env_path)
    monkeypatch.setattr(settings.lm, "anthropic_api_key", None)
    monkeypatch.setattr(settings.lm, "deepseek_api_key", None)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    return env_path


def test_plugin_key_ignores_legacy_settings_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider plugins use their declared env channel, never the Settings alias."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["deepseek-"] == ("DeepSeek", None, "DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(model="deepseek-v4-pro", config={})


def test_gemini_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated Gemini binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "gemini_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["gemini-"] == ("Google", None, "GEMINI_API_KEY")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        validate_model_config(model="gemini-3.5-flash", config={})


def test_anthropic_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated Anthropic binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["claude-"] == ("Anthropic", None, "ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_model_config(model="claude-sonnet-5", config={})


def test_openai_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated OpenAI binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["gpt-"] == ("OpenAI", None, "OPENAI_API_KEY")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_model_config(model="gpt-5.6-sol", config={})


def test_qwen_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated Qwen binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "dashscope_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["qwen"] == ("Alibaba", None, "DASHSCOPE_API_KEY")
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        validate_model_config(model="qwen3.8-max", config={})


def test_glm_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated Zhipu binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "zhipu_api_key", "sk-test")
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    ensure_provider_plugins_loaded()

    assert provider_key_map()["glm-"] == ("Zhipu", None, "GLM_API_KEY")
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        validate_model_config(model="glm-5.2", config={})


def test_file_fallback_allows_key_after_gateway_pop(env_file: Path) -> None:
    """A plugin key declared in the cluster `.env` authorizes the model."""
    env_file.write_text("DEEPSEEK_API_KEY=sk-file-value\n")
    assert validate_model_config(model="deepseek-v4-pro", config={}) == "deepseek-v4-pro"


def test_missing_key_still_fails(env_file: Path) -> None:
    """Neither settings nor the .env file has the key → the 400 intent holds."""
    env_file.write_text("SOME_OTHER_KEY=x\n")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(model="deepseek-v4-pro", config={})


def test_unknown_model_still_fails(env_file: Path) -> None:
    env_file.write_text("DEEPSEEK_API_KEY=x\n")
    with pytest.raises(ValueError, match="unknown model"):
        validate_model_config(model="no-such-model-xyz", config={})


# ---------------------------------------------------------------------------
# model_supports_vision — the message-endpoint image capability gate
# ---------------------------------------------------------------------------


class TestModelSupportsVision:
    """The gate answers per-model from the registry, with the prefix table as
    fallback for unregistered ids. The deepseek family is the live case that
    forced the per-model media matrix: one multimodal member under a text-only
    prefix."""

    def test_registered_vision_model_passes(self) -> None:
        assert model_supports_vision("deepseek-v4-flash-vision-exp") is True

    def test_registered_text_only_deepseek_fails(self) -> None:
        # Same prefix as the vision model — the per-model media types, not the prefix,
        # decides: an image to a v4-flash agent must still 422 up front.
        assert model_supports_vision("deepseek-v4-flash") is False
        assert model_supports_vision("deepseek-v4-pro") is False

    def test_unregistered_id_falls_back_to_prefix(self) -> None:
        # config_overlay experiments and retired aliases keep the old prefix
        # behavior: vision-capable plugin ids pass, a DeepSeek id does not.
        assert model_supports_vision("claude-unknown-id") is True
        assert model_supports_vision("gemini-4-experiment") is True
        assert model_supports_vision("gpt-unknown-id") is True
        assert model_supports_vision("qwen3.8-unknown-id") is True
        assert model_supports_vision("deepseek-unknown-id") is False
