"""Provider plugin mechanism — the provider.py contract end to end.

A fixture plugin directory is written into the session's tmp AVA_HOME
(tests/conftest.py redirects AVA_HOME), so these tests exercise the real
discovery path (shared/plugins_config._discover_plugins) and the real loader
(shared/lm/_plugin_providers). Every test restores the module-level
registration state it mutated: MODELS + derived views, provider_api bindings,
plugin prices, stop vocabulary, the loader's once-flag, and the concurrency
key cache.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from shared import paths, plugins_config
from shared.lm import _plugin_providers as plugin_loader
from shared.lm import pricing, provider_api, stop
from shared.lm._concurrency import _invalidate_known_provider_keys_cache, known_provider_keys
from shared.lm._plugin_providers import _reset_loaded_for_tests, ensure_provider_plugins_loaded
from shared.lm.factory import (
    MODEL_CONTEXT_WINDOW,
    MODEL_KNOWLEDGE_CUTOFF,
    SUPPORTED_MODELS,
    build_chat_model,
    model_supports_vision,
    provider_key_of_model,
    validate_model_config,
)
from shared.lm.registry import (
    MODELS,
    ModelSpec,
    ModelTuning,
    _rebuild_derived_views,
    register_models,
)

_REPO_PROVIDER_PLUGINS = {
    "lm_alibaba",
    "lm_anthropic",
    "lm_deepseek",
    "lm_google",
    "lm_moonshot",
    "lm_openai",
    "lm_xiaomi",
    "lm_zhipu",
}

_REPO_MODEL_VENDORS = {
    "claude-fable-5": "anthropic",
    "claude-fable-5-1": "anthropic",
    "claude-haiku-4-5": "anthropic",
    "claude-haiku-4-5-20251001": "anthropic",
    "claude-opus-4-6": "anthropic",
    "claude-opus-4-7": "anthropic",
    "claude-opus-4-8": "anthropic",
    "claude-opus-5": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-sonnet-5": "anthropic",
    "deepseek-v4-flash": "deepseek",
    "deepseek-v4-flash-vision-exp": "deepseek",
    "deepseek-v4-pro": "deepseek",
    "gemini-2.5-flash": "google",
    "gemini-2.5-pro": "google",
    "gemini-3.1-pro-preview": "google",
    "gemini-3.5-flash": "google",
    "gemini-3.7-flash": "google",
    "gemini-3.8-flash": "google",
    "glm-5.2": "zhipu",
    "glm-5.3": "zhipu",
    "glm-5.3-flash": "zhipu",
    "gpt-5.4-mini": "openai",
    "gpt-5.5": "openai",
    "gpt-5.6-luna": "openai",
    "gpt-5.6-sol": "openai",
    "gpt-5.6-terra": "openai",
    "kimi-k3": "moonshot",
    "mimo-v2.5-pro": "xiaomi",
    "mimo-v2.5-pro-ultraspeed": "xiaomi",
    "qwen3.8-27b": "alibaba",
    "qwen3.8-flash": "alibaba",
    "qwen3.8-max": "alibaba",
}

_PLUGIN_SOURCE = """from langchain_core.language_models.fake_chat_models import FakeListChatModel

from shared.lm.provider_api import PriceRates, ProviderBinding, register
from shared.lm.registry import ModelSpec, ModelTuning
from shared.lm.stop import StopSpec

def _build(ctx):
    return FakeListChatModel(responses=["hello"])

register(
    ProviderBinding(
        prefix="{prefix}",
        display_name="{display}",
        key_env="{key_env}",
        build=_build,
        vision={vision},
        stop_spec={stop_spec},
    ),
    models={{
        {models}
    }},
    pricing={{
        {pricing}
    }},
)
"""

_MODEL_LINE = """\"{model}\": ModelSpec(
            provider={provider!r},
            spawnable=True,
            context_window=200_000,
            knowledge_cutoff="2026-01",
            effort_levels=("low", "high"),
            tuning=ModelTuning(reasoning_effort="high"),
            media_types={model_media_types},
            superseded_by={superseded_by!r},
        )"""

_PRICE_LINE = """\"{model}\": PriceRates(
            cache_miss=1.0, cache_hit=0.1, output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at="2026-08-22",
            vendor={vendor!r},
        )"""


@pytest.fixture
def provider_plugin() -> Generator[Callable[..., None], None, None]:
    """Write a fixture provider.py + enable config, then restore all
    module-level registration state after the test."""

    # Another test in the same process may have already triggered the
    # production loader. Reset the test-only once flag before creating this
    # fixture plugin, or its provider.py would never be discovered.
    loader_was_loaded = plugin_loader._STATE.loaded
    _reset_loaded_for_tests()
    models_snapshot = dict(MODELS)
    bindings_snapshot = dict(provider_api.REGISTRY.bindings)
    prices_snapshot = dict(pricing._PLUGIN_PRICES)
    stop_snapshot = dict(stop._BY_PROVIDER)
    for model_id in tuple(MODELS):
        if model_id.startswith(
            (
                "claude-",
                "deepseek-",
                "gemini-",
                "glm-",
                "gpt-",
                "kimi-",
                "mimo-",
                "qwen3.8-",
            )
        ):
            MODELS.pop(model_id)
            pricing._PLUGIN_PRICES.pop(model_id, None)
    for prefix in (
        "claude-",
        "deepseek-",
        "gemini-",
        "glm-",
        "gpt-",
        "kimi-",
        "mimo-",
        "qwen3.8-",
    ):
        provider_api.REGISTRY.bindings.pop(prefix, None)
    for provider_key in ("anthropic", "google_genai", "moonshot", "openai"):
        stop._BY_PROVIDER.pop(provider_key, None)
    _rebuild_derived_views()
    # Tests share one session AVA_HOME — remove anything this test created so
    # a later test's loader scan cannot see leftover plugin dirs.
    created: list[Path] = []

    def _write(
        prefix: str = "testp-",
        display: str = "TestProvider",
        key_env: str = "TESTP_API_KEY",
        vision: bool = False,
        model_vision: bool = False,
        stop_spec: str | None = None,
        model: str | None = "testp-1",
        with_price: bool = True,
        price_vendor: str | None = None,
        superseded_by: str | None = None,
        dir_name: str = "test_provider",
    ) -> None:
        plugin_dir = paths.plugins_dir() / dir_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        created.append(plugin_dir)
        models = (
            _MODEL_LINE.format(
                model=model,
                provider=prefix.rstrip("-"),
                model_media_types='frozenset({"image"})' if model_vision else "frozenset()",
                superseded_by=superseded_by,
            )
            if model
            else ""
        )
        pricing_line = (
            _PRICE_LINE.format(model=model, vendor=price_vendor) if model and with_price else ""
        )
        source = _PLUGIN_SOURCE.format(
            prefix=prefix,
            display=display,
            key_env=key_env,
            vision="True" if vision else "False",
            stop_spec=stop_spec if stop_spec is not None else "None",
            models=models,
            pricing=pricing_line,
        )
        (plugin_dir / "provider.py").write_text(source)
        # Discovery is keyed on plugin.py — a provider plugin ships one (an
        # empty stub here; it contributes nothing agent-side).
        (plugin_dir / "plugin.py").write_text("# provider plugin stub")

    yield _write

    for d in created:
        shutil.rmtree(d, ignore_errors=True)
    cfg = paths.ava_home() / "plugins_config.json"
    cfg.unlink(missing_ok=True)

    # Restore module-level registration state (the session shares one process).
    MODELS.clear()
    MODELS.update(models_snapshot)
    _rebuild_derived_views()
    provider_api.REGISTRY.bindings.clear()
    provider_api.REGISTRY.bindings.update(bindings_snapshot)
    pricing._PLUGIN_PRICES.clear()
    pricing._PLUGIN_PRICES.update(prices_snapshot)
    stop._BY_PROVIDER.clear()
    stop._BY_PROVIDER.update(stop_snapshot)
    _reset_loaded_for_tests()
    plugin_loader._STATE.loaded = loader_was_loaded
    _invalidate_known_provider_keys_cache()
    from shared.env_registry import seed_allowlist

    seed_allowlist.cache_clear()


def test_repo_provider_plugins_are_the_exact_default_enabled_set() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))
    repo_root = paths.repo_plugins_dir().resolve()
    repo_provider_plugins = {
        name
        for name, plugin_dir in discovered.items()
        if plugin_dir.resolve().parent == repo_root
        and name.startswith("lm_")
        and (plugin_dir / "provider.py").is_file()
    }

    assert repo_provider_plugins == _REPO_PROVIDER_PLUGINS
    assert {
        name for name in repo_provider_plugins if config.plugins[name].enabled
    } == _REPO_PROVIDER_PLUGINS


def test_zero_provider_plugins_fail_loud_and_remain_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader_was_loaded = plugin_loader._STATE.loaded

    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(provider_api.REGISTRY, "bindings", {})
            isolated.setattr(plugins_config, "_discover_plugins", dict)
            _reset_loaded_for_tests()

            with pytest.raises(RuntimeError, match="no provider plugins enabled"):
                ensure_provider_plugins_loaded()
            assert not plugin_loader._STATE.loaded
    finally:
        _reset_loaded_for_tests()
        plugin_loader._STATE.loaded = loader_was_loaded


def test_repo_model_vendor_vocabulary_is_complete() -> None:
    ensure_provider_plugins_loaded()

    assert len(_REPO_MODEL_VENDORS) == 33
    assert set(MODELS) == _REPO_MODEL_VENDORS.keys()
    assert set(pricing._CATALOG) == {"gemini-embedding-2"}
    assert {
        model: pricing.model_vendor(model) for model in pricing._PLUGIN_PRICES
    } == _REPO_MODEL_VENDORS


def test_repo_plugin_prices_equal_archive_at_frozen_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_provider_plugins_loaded()
    plugin_prices = dict(pricing._PLUGIN_PRICES)
    archive_raw = json.loads(
        (Path(__file__).resolve().parents[2] / "shared/lm/pricing_catalog_archive.json").read_text()
    )
    archive_models = archive_raw["models"]
    monkeypatch.setattr(pricing, "_CATALOG", pricing._parse_catalog(archive_raw))
    monkeypatch.setattr(pricing, "_PLUGIN_PRICES", {})
    frozen_instant = datetime(2026, 9, 5, tzinfo=UTC)

    assert set(plugin_prices) == _REPO_MODEL_VENDORS.keys()
    for model, plugin_price in plugin_prices.items():
        plugin_rates = plugin_price.rates_at(frozen_instant, input_tokens=0)
        archive_rates = pricing.rates_at(model, frozen_instant, input_tokens=0)
        assert plugin_rates is not None and archive_rates is not None
        assert plugin_rates.as_tuple() == pytest.approx(archive_rates.as_tuple())  # pyright: ignore[reportUnknownMemberType]
        assert plugin_price.vendor == archive_models[model]["vendor"]
        assert (plugin_price.source_url, plugin_price.source_checked_at.isoformat()) == (
            archive_models[model]["source_url"],
            archive_models[model]["source_checked_at"],
        )


def test_repo_deepseek_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_deepseek"].enabled
    ensure_provider_plugins_loaded()

    deepseek_models = {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }
    assert deepseek_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["deepseek"]) == deepseek_models
    assert pricing.model_vendor("deepseek-v4-pro") == "deepseek"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map

    assert "deepseek-" not in _MODEL_KEY_MAP
    assert provider_key_map()["deepseek-"] == ("DeepSeek", None, "DEEPSEEK_API_KEY")
    binding = provider_api.REGISTRY.bindings["deepseek-"]
    assert binding.effort_levels == ("high", "max")
    assert binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_repo_google_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_google"].enabled
    ensure_provider_plugins_loaded()

    gemini_models = {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    }
    assert gemini_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["gemini"]) == {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
    }
    assert pricing.model_vendor("gemini-3.8-flash") == "google"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map

    assert "gemini-" not in _MODEL_KEY_MAP
    assert provider_key_map()["gemini-"] == ("Google", None, "GEMINI_API_KEY")
    binding = provider_api.REGISTRY.bindings["gemini-"]
    assert binding.effort_levels == ("minimal", "low", "medium", "high")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "google_genai",
        "finish_reason",
        frozenset({"STOP"}),
        frozenset({"MAX_TOKENS"}),
    )


def test_repo_anthropic_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_anthropic"].enabled
    ensure_provider_plugins_loaded()

    claude_models = {
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-haiku-4-5",
    }
    assert claude_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["claude"]) == {
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
        "claude-fable-5",
        "claude-fable-5-1",
    }
    assert pricing.model_vendor("claude-sonnet-5") == "anthropic"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map

    assert "claude-" not in _MODEL_KEY_MAP
    assert provider_key_map()["claude-"] == ("Anthropic", None, "ANTHROPIC_API_KEY")
    binding = provider_api.REGISTRY.bindings["claude-"]
    assert binding.effort_levels is None
    assert binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "anthropic",
        "stop_reason",
        frozenset({"end_turn", "tool_use", "refusal"}),
        frozenset({"max_tokens"}),
    )


def test_repo_openai_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_openai"].enabled
    ensure_provider_plugins_loaded()

    gpt_models = {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4-mini",
    }
    assert gpt_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["gpt"]) == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
    assert pricing.model_vendor("gpt-5.6-sol") == "openai"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map

    assert "gpt-" not in _MODEL_KEY_MAP
    assert provider_key_map()["gpt-"] == ("OpenAI", None, "OPENAI_API_KEY")
    binding = provider_api.REGISTRY.bindings["gpt-"]
    assert binding.effort_levels == ("none", "low", "medium", "high", "xhigh", "max")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "openai",
        "finish_reason",
        frozenset({"stop", "tool_calls", "function_call"}),
        frozenset({"length"}),
        status_key="status",
        status_map={
            "completed": stop.StopCategory.NORMAL,
            "incomplete": stop.StopCategory.TRUNCATED,
        },
    )


def test_repo_alibaba_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_alibaba"].enabled
    ensure_provider_plugins_loaded()

    qwen_models = {
        "qwen3.8-max",
        "qwen3.8-27b",
        "qwen3.8-flash",
    }
    assert qwen_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["qwen"]) == qwen_models
    assert pricing.model_vendor("qwen3.8-max") == "alibaba"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map

    assert "qwen" not in _MODEL_KEY_MAP
    assert provider_key_map()["qwen"] == ("Alibaba", None, "DASHSCOPE_API_KEY")
    binding = provider_api.REGISTRY.bindings["qwen3.8-"]
    assert binding.provider_key == "qwen"
    assert binding.effort_levels == ("none", "high")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec is None


def test_repo_zhipu_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_zhipu"].enabled
    ensure_provider_plugins_loaded()

    glm_models = {
        "glm-5.2",
        "glm-5.3",
        "glm-5.3-flash",
    }
    assert glm_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["glm"]) == glm_models
    assert pricing.model_vendor("glm-5.2") == "zhipu"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map, provider_key_of_model

    assert "glm-" not in _MODEL_KEY_MAP
    assert provider_key_map()["glm-"] == ("Zhipu", None, "GLM_API_KEY")
    assert provider_key_of_model("glm-5.2") == "glm"
    binding = provider_api.REGISTRY.bindings["glm-"]
    assert binding.prefix == "glm-"
    assert binding.provider_key is None
    assert binding.effort_levels == ("low", "high", "max")
    assert not binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_repo_moonshot_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_moonshot"].enabled
    ensure_provider_plugins_loaded()

    assert "kimi-k3" in MODELS
    assert set(SUPPORTED_MODELS["kimi"]) == {"kimi-k3"}
    assert pricing.model_vendor("kimi-k3") == "moonshot"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map, provider_key_of_model

    assert "kimi-" not in _MODEL_KEY_MAP
    assert provider_key_map()["kimi-"] == ("Moonshot", None, "MOONSHOT_API_KEY")
    assert provider_key_of_model("kimi-k3") == "kimi"
    binding = provider_api.REGISTRY.bindings["kimi-"]
    assert binding.prefix == "kimi-"
    assert binding.provider_key is None
    assert binding.effort_levels == ("low", "high", "max")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "moonshot",
        "finish_reason",
        frozenset({"stop", "tool_calls", "function_call"}),
        frozenset({"length"}),
    )


def test_repo_xiaomi_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_xiaomi"].enabled
    ensure_provider_plugins_loaded()

    mimo_models = {
        "mimo-v2.5-pro",
        "mimo-v2.5-pro-ultraspeed",
    }
    assert mimo_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["mimo"]) == mimo_models
    assert pricing.model_vendor("mimo-v2.5-pro") == "xiaomi"

    from shared.lm.factory import _MODEL_KEY_MAP, provider_key_map, provider_key_of_model

    assert _MODEL_KEY_MAP == {}
    assert provider_key_map()["mimo-"] == ("Xiaomi", None, "MIMO_API_KEY")
    assert provider_key_of_model("mimo-v2.5-pro") == "mimo"
    binding = provider_api.REGISTRY.bindings["mimo-"]
    assert binding.prefix == "mimo-"
    assert binding.provider_key is None
    assert binding.effort_levels == ("none", "high")
    assert not binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_plugin_model_registers_and_builds(provider_plugin: Callable[..., None]) -> None:
    provider_plugin()
    ensure_provider_plugins_loaded()

    assert "testp-1" in MODELS
    assert "testp-1" in SUPPORTED_MODELS["testp"]
    assert MODEL_CONTEXT_WINDOW["testp-1"] == 200_000
    assert MODEL_KNOWLEDGE_CUTOFF["testp-1"] == "2026-01"
    assert provider_key_of_model("testp-1") == "testp"
    assert "testp" in known_provider_keys()
    assert "testp-1" in pricing.MODEL_PRICING
    assert next(iter(pricing.RETIRED_MODEL_PRICING)) not in pricing.MODEL_PRICING

    llm = build_chat_model("testp-1")
    assert isinstance(llm, FakeListChatModel)
    # An id under the plugin's prefix that has no registry entry still
    # dispatches (matching the repo provider plugins): the builder receives
    # spec=None and decides its own posture.
    llm2 = build_chat_model("testp-2")
    assert isinstance(llm2, FakeListChatModel)


@pytest.mark.parametrize(
    ("price_vendor", "expected"), [("test-vendor", "test-vendor"), (None, None)]
)
def test_plugin_price_vendor_reaches_pricing_lookup(
    provider_plugin: Callable[..., None],
    price_vendor: str | None,
    expected: str | None,
) -> None:
    provider_plugin(price_vendor=price_vendor)
    ensure_provider_plugins_loaded()

    assert pricing._PLUGIN_PRICES["testp-1"].vendor == expected
    assert pricing.model_vendor("testp-1") == expected
    assert pricing.model_vendor("testp-unpriced") is None


def test_plugin_binding_effort_levels_reach_build_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[provider_api.BuildContext] = []

    def _build(ctx: provider_api.BuildContext) -> FakeListChatModel:
        contexts.append(ctx)
        return FakeListChatModel(responses=["hello"])

    binding = provider_api.ProviderBinding(
        prefix="testctx-",
        display_name="Test Context",
        key_env="TESTCTX_API_KEY",
        build=_build,
        effort_levels=("low", "high"),
    )
    monkeypatch.setitem(provider_api.REGISTRY.bindings, binding.prefix, binding)
    monkeypatch.setattr("shared.lm.factory.ensure_provider_plugins_loaded", lambda: None)

    assert isinstance(
        build_chat_model(
            "testctx-model",
            media_resolution="high",
            media_thinking_level="low",
            base_url="https://example.com/v1",
        ),
        FakeListChatModel,
    )
    assert contexts[0].effort_levels == ("low", "high")
    assert contexts[0].media_resolution == "high"
    assert contexts[0].media_thinking_level == "low"
    assert contexts[0].base_url == "https://example.com/v1"


def test_plugin_model_validation_and_key_check(
    provider_plugin: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_plugin()
    ensure_provider_plugins_loaded()

    monkeypatch.setattr("shared.runtime_config.read_env_aliases", dict)
    with pytest.raises(ValueError, match="TESTP_API_KEY"):
        validate_model_config(model="testp-1")

    monkeypatch.setattr(
        "shared.runtime_config.read_env_aliases",
        lambda: {"TESTP_API_KEY": "sk-test"},
    )
    assert validate_model_config(model="testp-1") == "testp-1"


def test_vision_flag_drives_image_gate(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(prefix="testv-", model="testv-1", vision=True)
    assert model_supports_vision("testv-unregistered")
    assert not model_supports_vision("testp-9")


def test_registered_plugin_model_vision_overrides_binding(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(
        prefix="testvtrue-",
        model="testvtrue-1",
        vision=False,
        model_vision=True,
        dir_name="vision_true",
    )
    provider_plugin(
        prefix="testvfalse-",
        model="testvfalse-1",
        vision=True,
        model_vision=False,
        dir_name="vision_false",
    )

    assert model_supports_vision("testvtrue-1")
    assert not model_supports_vision("testvfalse-1")


def test_duplicate_prefix_rejected(provider_plugin: Callable[..., None]) -> None:
    provider_plugin()
    # Re-registration under the same prefix (a second provider.py) fails fast.
    provider_plugin(
        prefix="testp-",
        display="Other",
        key_env="OTHER_API_KEY",
        model="testp-other",
        dir_name="test_provider2",
    )
    with pytest.raises(RuntimeError) as excinfo:
        ensure_provider_plugins_loaded()
    assert "already claimed" in str(excinfo.value.__cause__)
    assert "testp-other" not in MODELS


def test_nested_prefix_rejected(provider_plugin: Callable[..., None]) -> None:
    provider_plugin()
    ensure_provider_plugins_loaded()
    with pytest.raises(ValueError, match="nests inside"):
        provider_api.register(
            provider_api.ProviderBinding(
                prefix="testp-sub-",
                display_name="Sub",
                key_env="SUB_API_KEY",
                build=lambda _ctx: FakeListChatModel(responses=["x"]),
            ),
            models={
                "testp-sub-1": ModelSpec(
                    provider="testp-sub",
                    spawnable=True,
                    context_window=100_000,
                    knowledge_cutoff="2026-01",
                    effort_levels=("low",),
                    tuning=ModelTuning(reasoning_effort="low"),
                )
            },
            pricing={
                "testp-sub-1": provider_api.PriceRates(
                    cache_miss=1.0,
                    cache_hit=0.1,
                    output=2.0,
                    source_url="https://example.com/pricing",
                    source_checked_at="2026-08-22",
                )
            },
        )


def test_core_prefix_cannot_be_shadowed(provider_plugin: Callable[..., None]) -> None:
    reserved = set(provider_api.REGISTRY._reserved_prefixes)
    provider_api.REGISTRY.reserve_core_prefixes({"core-"})
    try:
        with pytest.raises(ValueError, match="already claimed"):
            provider_api.register(
                provider_api.ProviderBinding(
                    prefix="core-",
                    display_name="Shadow",
                    key_env="X",
                    build=lambda _ctx: FakeListChatModel(responses=["x"]),
                ),
                models={},
                pricing={},
            )
    finally:
        provider_api.REGISTRY.reserve_core_prefixes(reserved)


def test_loader_reserves_core_prefixes_before_bootstrap_can_load_a_plugin(
    provider_plugin: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap may be the first provider consumer, before factory import setup."""
    from shared.lm import factory

    reserved = set(provider_api.REGISTRY._reserved_prefixes)
    provider_api.REGISTRY._reserved_prefixes.clear()
    monkeypatch.setattr(factory, "_MODEL_KEY_MAP", {"core-": ("Core", "core_key", "CORE_KEY")})
    provider_plugin(prefix="core-", model="core-test")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            ensure_provider_plugins_loaded()
        assert "already claimed" in str(excinfo.value.__cause__)
    finally:
        provider_api.REGISTRY._reserved_prefixes.clear()
        provider_api.REGISTRY._reserved_prefixes.update(reserved)


def test_plugin_model_id_must_match_binding_prefix() -> None:
    with pytest.raises(ValueError, match="must start with"):
        provider_api.register(
            provider_api.ProviderBinding(
                prefix="testp-",
                display_name="TestProvider",
                key_env="TESTP_API_KEY",
                build=lambda _ctx: FakeListChatModel(responses=["x"]),
            ),
            models={
                "wrong-1": ModelSpec(
                    provider="testp",
                    spawnable=True,
                    context_window=100_000,
                    knowledge_cutoff="2026-01",
                    effort_levels=("low",),
                    tuning=ModelTuning(reasoning_effort="low"),
                )
            },
            pricing={
                "wrong-1": provider_api.PriceRates(
                    cache_miss=1.0,
                    cache_hit=0.1,
                    output=2.0,
                    source_url="https://example.com/pricing",
                    source_checked_at="2026-08-22",
                )
            },
        )


def test_plugin_price_must_name_registered_model() -> None:
    with pytest.raises(ValueError, match="unregistered models"):
        provider_api.register(
            provider_api.ProviderBinding(
                prefix="testp-",
                display_name="TestProvider",
                key_env="TESTP_API_KEY",
                build=lambda _ctx: FakeListChatModel(responses=["x"]),
            ),
            models={},
            pricing={
                "testp-1": provider_api.PriceRates(
                    cache_miss=1.0,
                    cache_hit=0.1,
                    output=2.0,
                    source_url="https://example.com/pricing",
                    source_checked_at="2026-08-22",
                )
            },
        )


def test_duplicate_model_id_rejected() -> None:
    with pytest.raises(RuntimeError, match="already registered"):
        register_models("testp", {"mimo-v2.5-pro": ModelSpec(provider="testp")})


def test_spawnable_model_without_price_rejected(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(with_price=False)
    with pytest.raises(RuntimeError) as excinfo:
        ensure_provider_plugins_loaded()
    assert "no current price" in str(excinfo.value.__cause__)


@pytest.mark.usefixtures("provider_plugin")
def test_model_validation_failure_leaves_registration_retryable() -> None:
    binding = provider_api.ProviderBinding(
        prefix="testp-",
        display_name="TestProvider",
        key_env="TESTP_API_KEY",
        build=lambda _ctx: FakeListChatModel(responses=["x"]),
    )
    price = provider_api.PriceRates(
        cache_miss=1.0,
        cache_hit=0.1,
        output=3.0,
        source_url="https://example.com/pricing",
        source_checked_at="2026-08-22",
    )
    invalid = ModelSpec(provider="testp", spawnable=True)

    with pytest.raises(RuntimeError, match="missing registry facts"):
        provider_api.register(binding, models={"testp-1": invalid}, pricing={"testp-1": price})

    assert "testp-1" not in pricing._PLUGIN_PRICES
    assert "testp-1" not in MODELS
    assert "testp-" not in provider_api.REGISTRY.bindings

    valid = ModelSpec(
        provider="testp",
        spawnable=True,
        context_window=200_000,
        knowledge_cutoff="2026-01",
        effort_levels=("low", "high"),
        tuning=ModelTuning(reasoning_effort="high"),
    )
    provider_api.register(binding, models={"testp-1": valid}, pricing={"testp-1": price})

    assert "testp-1" in pricing._PLUGIN_PRICES
    assert MODELS["testp-1"] == valid
    assert provider_api.REGISTRY.bindings["testp-"] == binding


def test_loader_revalidates_cross_model_constraints(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(superseded_by="testp-missing")

    with pytest.raises(RuntimeError, match="not in MODELS"):
        ensure_provider_plugins_loaded()

    assert not plugin_loader._STATE.loaded


@pytest.mark.parametrize(
    ("cache_miss", "source_checked_at", "error"),
    [
        (math.nan, "2026-08-22", "finite and non-negative"),
        (1.0, "2026-8-22", "source_checked_at"),
    ],
)
def test_plugin_price_validation(cache_miss: float, source_checked_at: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        pricing.register_plugin_price(
            "invalid-plugin-price",
            cache_miss=cache_miss,
            cache_hit=0.1,
            output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at=source_checked_at,
        )


def test_stop_spec_registration_reaches_classify_stop(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(
        prefix="tests-",
        model="tests-1",
        stop_spec='StopSpec("testsdk", "finish_reason", frozenset({"stop"}), frozenset({"length"}))',
    )
    ensure_provider_plugins_loaded()
    from shared.lm.stop import classify_stop

    category, raw = classify_stop(
        AIMessage(
            content="", response_metadata={"model_provider": "testsdk", "finish_reason": "stop"}
        )
    )
    assert category.name == "NORMAL" and raw == "stop"
    category, raw = classify_stop(
        AIMessage(
            content="", response_metadata={"model_provider": "testsdk", "finish_reason": "length"}
        )
    )
    assert category.name == "TRUNCATED"


def test_disabled_plugin_skipped(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    provider_plugin(dir_name="disabled_provider")
    cfg_path = paths.ava_home() / "plugins_config.json"
    cfg_path.write_text(json.dumps({"plugins": {"disabled_provider": {"enabled": False}}}))
    ensure_provider_plugins_loaded()
    assert "testp-1" not in MODELS
    assert "kept-1" in MODELS


def test_provider_missing_provider_py_is_noop(provider_plugin: Callable[..., None]) -> None:
    # A plugin dir with only plugin.py (the common case) registers nothing.
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    plugin_dir = paths.plugins_dir() / "no_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text("# empty")
    ensure_provider_plugins_loaded()
    assert "kept-1" in MODELS
    assert "no_provider-1" not in MODELS


def test_provider_only_dir_is_not_a_plugin(provider_plugin: Callable[..., None]) -> None:
    # A dir with provider.py but no plugin.py is not discovered — discovery
    # identity is plugin.py, documented in provider_api.
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    plugin_dir = paths.plugins_dir() / "orphan_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "provider.py").write_text("# no plugin.py beside me")
    ensure_provider_plugins_loaded()
    assert "kept-1" in MODELS
    assert "orphan-provider-1" not in MODELS
