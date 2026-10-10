"""Plugin provider key validation and model media capability tests.

Anthropic, DeepSeek, Gemini, Kimi, MIMO, OpenAI, Qwen, and Zhipu are provider plugins, so
their key declarations are plugin-owned. Spawn validation reads the unit's
effective channel — os.environ first (bootstrap-injected on pure runners,
dotenv-loaded on the gateway), then the local `.env` file fallback for the
gateway profile that pops provider keys from the process env. The legacy
Settings fields stay for configuration compatibility but no longer authorize
a spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.factory import model_supports_vision, provider_key_map, validate_model_config
from tests.fixtures.model_catalog import AddModels


@pytest.fixture
def env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the plugin key reader at a scratch cluster `.env`."""
    import base.host.env.runtime_config as rc

    env_path = tmp_path / ".env"
    monkeypatch.setattr(rc, "env_file_path", lambda: env_path)
    monkeypatch.setattr(settings.lm, "anthropic_api_key", None)
    monkeypatch.setattr(settings.lm, "deepseek_api_key", None)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    return env_path


def test_plugin_key_env_injection_authorizes_without_env_file(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, *, model_catalog: ModelCatalog
) -> None:
    """Runner topology: the key arrives in os.environ only (bootstrap
    injection; cluster facts are not materialized into the runner's .env)."""
    env_file.write_text("")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")

    assert (
        validate_model_config(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )
        == "deepseek-flash"
    )


def test_plugin_key_missing_in_both_channels_raises(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, *, model_catalog: ModelCatalog
) -> None:
    """Neither the process env nor the .env file carries the key — fail fast."""
    env_file.write_text("")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """Provider plugins use their declared env channel, never the Settings alias."""
    monkeypatch.setattr(settings.lm, "deepseek_api_key", "sk-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["deepseek-"] == ("DeepSeek", "DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_gemini_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Gemini binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "gemini_api_key", "sk-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["gemini-"] == ("Google", "GEMINI_API_KEY")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        validate_model_config(
            model="gemini-3.5-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_anthropic_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Anthropic binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "anthropic_api_key", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["claude-"] == ("Anthropic", "ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_model_config(
            model="claude-sonnet-5",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_openai_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated OpenAI binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "openai_api_key", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["gpt-"] == ("OpenAI", "OPENAI_API_KEY")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_model_config(
            model="gpt-5.6-sol",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_qwen_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Qwen binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "dashscope_api_key", "sk-test")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["qwen"] == ("Alibaba", "DASHSCOPE_API_KEY")
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        validate_model_config(
            model="qwen3.8-max",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_glm_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Zhipu binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "zhipu_api_key", "sk-test")
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["glm-"] == ("Zhipu", "GLM_API_KEY")
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        validate_model_config(
            model="glm-5.2", config={}, catalog=model_catalog, llm_override=settings.lm.llm_override
        )


def test_kimi_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Moonshot binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "moonshot_api_key", "sk-test")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["kimi-"] == ("Moonshot", "MOONSHOT_API_KEY")
    with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
        validate_model_config(
            model="kimi-k3", config={}, catalog=model_catalog, llm_override=settings.lm.llm_override
        )


def test_mimo_plugin_key_ignores_legacy_settings_field(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The migrated Xiaomi binding also reads only its declared env channel."""
    monkeypatch.setattr(settings.lm, "xiaomi_api_key", "sk-test")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(settings.lm, "llm_override", "")
    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)

    assert provider_key_map(catalog=model_catalog)["mimo-"] == ("Xiaomi", "MIMO_API_KEY")
    with pytest.raises(ValueError, match="MIMO_API_KEY"):
        validate_model_config(
            model="mimo-v2.5-pro",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_file_fallback_allows_key_after_gateway_pop(
    env_file: Path, *, model_catalog: ModelCatalog
) -> None:
    """A plugin key declared in the cluster `.env` authorizes the model."""
    env_file.write_text("DEEPSEEK_API_KEY=sk-file-value\n")
    assert (
        validate_model_config(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )
        == "deepseek-flash"
    )


def test_missing_key_still_fails(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """Neither channel (os.environ nor the .env file) has the key → the 400 intent holds."""
    env_file.write_text("SOME_OTHER_KEY=x\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        validate_model_config(
            model="deepseek-flash",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_withdrawn_model_resolves_to_its_fallback_at_the_spawn_boundary(
    env_file: Path, add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    """A synthetic withdrawal preserves fallback coverage at both spawn inputs."""
    from dataclasses import replace

    env_file.write_text("DEEPSEEK_API_KEY=sk-file-value\n")
    model = "deepseek-retired-fixture"
    model_catalog = add_models(
        model_catalog,
        {
            model: replace(
                model_catalog.models["deepseek-flash"],
                spawnable=False,
                unavailable_fallback="deepseek-flash",
            )
        },
    )
    assert (
        validate_model_config(
            model=model, config={}, catalog=model_catalog, llm_override=settings.lm.llm_override
        )
        == "deepseek-flash"
    )
    assert (
        validate_model_config(
            model=None,
            config={"llm_model": model},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )
        == "deepseek-flash"
    )


@pytest.mark.parametrize(
    "model",
    (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5-pro-ultraspeed",
    ),
)
def test_removed_model_fails_spawn_validation(
    env_file: Path, model: str, *, model_catalog: ModelCatalog
) -> None:
    env_file.write_text("DEEPSEEK_API_KEY=sk-file-value\n")
    with pytest.raises(ValueError, match=f"unknown model '{model}'"):
        validate_model_config(
            model=model, config={}, catalog=model_catalog, llm_override=settings.lm.llm_override
        )
    with pytest.raises(ValueError, match=f"unknown model '{model}'"):
        validate_model_config(
            model=None,
            config={"llm_model": model},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


def test_unknown_model_still_fails(env_file: Path, *, model_catalog: ModelCatalog) -> None:
    env_file.write_text("DEEPSEEK_API_KEY=x\n")
    with pytest.raises(ValueError, match="unknown model"):
        validate_model_config(
            model="no-such-model-xyz",
            config={},
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
        )


# ---------------------------------------------------------------------------
# model_supports_vision — the message-endpoint image capability gate
# ---------------------------------------------------------------------------


class TestModelSupportsVision:
    """The gate answers from registry media types or plugin prefix fallback."""

    def test_registered_vision_model_passes(self, *, model_catalog: ModelCatalog) -> None:
        assert model_supports_vision("glm-5.3-flash", catalog=model_catalog) is True

    def test_registered_text_only_deepseek_fails(self, *, model_catalog: ModelCatalog) -> None:
        # An image to a text-only flash agent must still 422 up front.
        assert model_supports_vision("deepseek-flash", catalog=model_catalog) is False

    def test_unregistered_id_falls_back_to_prefix(self, *, model_catalog: ModelCatalog) -> None:
        # config_overlay experiments and retired aliases keep the old prefix
        # behavior: vision-capable plugin ids pass, a DeepSeek id does not.
        assert model_supports_vision("claude-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("gemini-4-experiment", catalog=model_catalog) is True
        assert model_supports_vision("gpt-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("qwen3.8-unknown-id", catalog=model_catalog) is True
        assert model_supports_vision("deepseek-unknown-id", catalog=model_catalog) is False
