"""Provider plugin mechanism — the provider.py contract end to end.

A fixture plugin directory is written into the session's tmp AVA_HOME
(tests/fixtures/env_bootstrap.py redirects AVA_HOME), so these tests exercise the real
discovery path (base/plugins_config.discover_plugins) and the real loader
(base/lm/plugin_providers). The provider-plugin tests run against an empty
catalog slot (`provider_plugin`), so the process's own catalog is untouched.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from base import paths
from base.lm import plugin_providers as plugin_loader
from base.lm import pricing, provider_api, stop
from base.lm.catalog import CatalogBuilder
from base.lm.factory import (
    build_chat_model,
    model_supports_vision,
    provider_key_of_model,
    validate_model_config,
)
from base.lm.plugin_providers import model_catalog, use_catalog
from base.lm.provider_api import PriceRates, ProviderBinding, ProviderContribution
from base.lm.registry import ModelSpec, ModelTuning
from base.lm.tests.provider_plugin_support import provider_plugin as provider_plugin
from base.packages.plugins import enable_config
from tests.fixtures.model_catalog import AddBindings

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
    "claude-haiku-4-5-20251001": "anthropic",
    "claude-opus-5": "anthropic",
    "claude-opus-5-5": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-sonnet-5-5": "anthropic",
    "deepseek-flash": "deepseek",
    "gemini-3.1-pro-preview": "google",
    "gemini-3.5-flash": "google",
    "gemini-3.7-flash": "google",
    "gemini-3.8-flash": "google",
    "gemini-flash-lite-latest": "google",
    "glm-5.2": "zhipu",
    "glm-5.3": "zhipu",
    "glm-5.3-flash": "zhipu",
    "glm-5.3-flashx": "zhipu",
    "gpt-5.6-luna": "openai",
    "gpt-5.6-sol": "openai",
    "gpt-5.6-terra": "openai",
    "gpt-6-astra": "openai",
    "gpt-6-sol": "openai",
    "gpt-6.1-sol": "openai",
    "gpt-6-luna": "openai",
    "kimi-k3": "moonshot",
    "mimo-v2.5-pro": "xiaomi",
    "mimo-v2.6-pro": "xiaomi",
    "mimo-v2.6-pro-ultraspeed": "xiaomi",
    "qwen3.8-27b": "alibaba",
    "qwen3.8-flash": "alibaba",
    "qwen3.8-max": "alibaba",
}


def test_repo_provider_plugins_are_the_exact_default_enabled_set() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))
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
    with use_catalog(None), monkeypatch.context() as isolated:
        isolated.setattr(enable_config, "discover_plugins", dict)

        with pytest.raises(RuntimeError, match="no provider plugins enabled"):
            model_catalog()
        assert plugin_loader._STATE.catalog is None


def test_repo_model_vendor_vocabulary_is_complete() -> None:
    assert set(model_catalog().models) == _REPO_MODEL_VENDORS.keys()
    # Catalog-only entries: a registered chat model pops its archive entry, so
    # what remains is the catalog-only services plus models the registry no
    # longer carries — historical usage stays priceable from the archive.
    assert set(model_catalog().prices.archive) == {
        "gemini-embedding-2",
        "deepseek-v4.1-flash-expires-on-0910",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5-pro-ultraspeed",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gpt-5.5",
        "gpt-5.4-mini",
    }
    assert {
        model: pricing.model_vendor(model) for model in model_catalog().prices.plugin
    } == _REPO_MODEL_VENDORS


def test_repo_plugin_prices_equal_archive_at_frozen_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_prices = dict(model_catalog().prices.plugin)
    archive_raw = json.loads(
        (Path(__file__).resolve().parents[3] / "base/lm/pricing_catalog_archive.json").read_text()
    )
    archive_models = archive_raw["models"]
    archive_only = pricing.PriceBook(pricing._parse_catalog(archive_raw), {})
    frozen_instant = datetime(2026, 9, 5, tzinfo=UTC)
    assert set(plugin_prices) == _REPO_MODEL_VENDORS.keys()
    for model, plugin_price in plugin_prices.items():
        plugin_rates = plugin_price.rates_at(frozen_instant, input_tokens=0)
        archive_rates = archive_only.rates_at(model, frozen_instant, input_tokens=0)
        assert plugin_rates is not None and archive_rates is not None
        assert plugin_rates.as_tuple() == pytest.approx(archive_rates.as_tuple())  # pyright: ignore[reportUnknownMemberType]
        assert plugin_price.vendor == archive_models[model]["vendor"]
        assert (plugin_price.source_url, plugin_price.source_checked_at.isoformat()) == (
            archive_models[model]["source_url"],
            archive_models[model]["source_checked_at"],
        )


def _install(
    binding: ProviderBinding,
    *,
    models: dict[str, ModelSpec],
    pricing: dict[str, PriceRates],
    builder: CatalogBuilder | None = None,
) -> CatalogBuilder:
    """Install a hand-built provider declaration the way the loader does for a plugin."""
    builder = builder or CatalogBuilder()
    builder.install("test", ProviderContribution(binding, models, pricing))
    return builder


def test_repo_deepseek_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_deepseek"].enabled
    model_catalog()

    assert {model for model in model_catalog().models if model.startswith("deepseek-")} == {
        "deepseek-flash"
    }
    assert set(model_catalog().supported_models["deepseek"]) == {"deepseek-flash"}
    assert "deepseek-v4-pro" not in model_catalog().prices.plugin
    assert pricing.model_vendor("deepseek-v4-pro") == "deepseek"

    from base.lm.factory import provider_key_map

    assert provider_key_map()["deepseek-"] == ("DeepSeek", "DEEPSEEK_API_KEY")
    binding = model_catalog().bindings["deepseek-"]
    assert binding.effort_levels == ("high", "max")
    assert binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_repo_google_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_google"].enabled
    model_catalog()

    gemini_models = {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-flash-lite-latest",
        "gemini-3.1-pro-preview",
    }
    assert gemini_models <= model_catalog().models.keys()
    assert set(model_catalog().supported_models["gemini"]) == {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-flash-lite-latest",
        "gemini-3.1-pro-preview",
    }
    assert pricing.model_vendor("gemini-3.8-flash") == "google"

    from base.lm.factory import provider_key_map

    assert provider_key_map()["gemini-"] == ("Google", "GEMINI_API_KEY")
    binding = model_catalog().bindings["gemini-"]
    assert binding.effort_levels == ("minimal", "low", "medium", "high")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "google_genai",
        "finish_reason",
        frozenset({"STOP"}),
        frozenset({"MAX_TOKENS"}),
    )


def test_repo_alibaba_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_alibaba"].enabled
    model_catalog()

    qwen_models = {
        "qwen3.8-max",
        "qwen3.8-27b",
        "qwen3.8-flash",
    }
    assert qwen_models <= model_catalog().models.keys()
    assert set(model_catalog().supported_models["qwen"]) == qwen_models
    assert pricing.model_vendor("qwen3.8-max") == "alibaba"

    from base.lm.factory import provider_key_map

    assert provider_key_map()["qwen"] == ("Alibaba", "DASHSCOPE_API_KEY")
    binding = model_catalog().bindings["qwen3.8-"]
    assert binding.provider_key == "qwen"
    assert binding.effort_levels == ("none", "high")
    assert not binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec is None


def test_repo_zhipu_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_zhipu"].enabled
    model_catalog()

    glm_models = {
        "glm-5.2",
        "glm-5.3",
        "glm-5.3-flash",
        "glm-5.3-flashx",
    }
    assert glm_models <= model_catalog().models.keys()
    assert set(model_catalog().supported_models["glm"]) == glm_models
    assert pricing.model_vendor("glm-5.2") == "zhipu"

    from base.lm.factory import provider_key_map, provider_key_of_model

    assert provider_key_map()["glm-"] == ("Zhipu", "GLM_API_KEY")
    assert provider_key_of_model("glm-5.2") == "glm"
    binding = model_catalog().bindings["glm-"]
    assert binding.prefix == "glm-"
    assert binding.provider_key is None
    assert binding.effort_levels == ("low", "high", "max")
    assert not binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_repo_moonshot_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_moonshot"].enabled
    model_catalog()

    assert "kimi-k3" in model_catalog().models
    assert set(model_catalog().supported_models["kimi"]) == {"kimi-k3"}
    assert pricing.model_vendor("kimi-k3") == "moonshot"

    from base.lm.factory import provider_key_map, provider_key_of_model

    assert provider_key_map()["kimi-"] == ("Moonshot", "MOONSHOT_API_KEY")
    assert provider_key_of_model("kimi-k3") == "kimi"
    binding = model_catalog().bindings["kimi-"]
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


def _assert_mimo_models_registered_with_pricing_vendor() -> None:
    mimo_models = {
        "mimo-v2.5-pro",
        "mimo-v2.6-pro",
        "mimo-v2.6-pro-ultraspeed",
    }
    assert {model for model in model_catalog().models if model.startswith("mimo-")} == mimo_models
    assert set(model_catalog().supported_models["mimo"]) == mimo_models
    assert model_catalog().models["mimo-v2.5-pro"].superseded_by == "mimo-v2.6-pro"
    assert "mimo-v2.5-pro-ultraspeed" not in model_catalog().prices.plugin
    assert pricing.model_vendor("mimo-v2.5-pro-ultraspeed") == "xiaomi"
    assert pricing.model_vendor("mimo-v2.5-pro") == "xiaomi"


def _assert_mimo_provider_key_and_binding() -> None:
    from base.lm.factory import provider_key_map, provider_key_of_model

    assert provider_key_map()["mimo-"] == ("Xiaomi", "MIMO_API_KEY")
    assert provider_key_of_model("mimo-v2.5-pro") == "mimo"
    binding = model_catalog().bindings["mimo-"]
    assert binding.prefix == "mimo-"
    assert binding.provider_key is None
    assert binding.effort_levels == ("none", "high")
    assert not binding.anthropic_protocol
    assert not binding.vision
    assert binding.stop_spec is None


def test_repo_xiaomi_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_xiaomi"].enabled
    model_catalog()

    _assert_mimo_models_registered_with_pricing_vendor()
    _assert_mimo_provider_key_and_binding()


def test_plugin_model_registers_and_builds(provider_plugin: Callable[..., None]) -> None:
    provider_plugin()
    model_catalog()

    assert "testp-1" in model_catalog().models
    assert "testp-1" in model_catalog().supported_models["testp"]
    assert model_catalog().context_windows["testp-1"] == 200_000
    assert model_catalog().knowledge_cutoffs["testp-1"] == "2026-01"
    assert provider_key_of_model("testp-1") == "testp"
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
    model_catalog()

    assert model_catalog().prices.plugin["testp-1"].vendor == expected
    assert pricing.model_vendor("testp-1") == expected
    assert pricing.model_vendor("testp-unpriced") is None


def test_plugin_binding_effort_levels_reach_build_context(add_bindings: AddBindings) -> None:
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
    add_bindings({binding.prefix: binding})

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
    model_catalog()

    monkeypatch.setattr("base.host.env.runtime_config.read_env_aliases", dict)
    with pytest.raises(ValueError, match="TESTP_API_KEY"):
        validate_model_config(model="testp-1")

    monkeypatch.setattr(
        "base.host.env.runtime_config.read_env_aliases",
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
    # Re-registration under the same prefix (a second provider.py) fails the
    # load: a registration-contract violation is fail-closed (not contained
    # like a code-load failure) — the flat prefix map cannot pick a winner.
    provider_plugin(
        prefix="testp-",
        display="Other",
        key_env="OTHER_API_KEY",
        model="testp-other",
        dir_name="test_provider2",
    )
    with pytest.raises(provider_api.ProviderRegistrationError, match="already claimed") as excinfo:
        model_catalog()
    assert "already claimed" in str(excinfo.value.__cause__)
    # A fail-closed load leaves no catalog behind; the next call builds (and fails) afresh.
    assert plugin_loader._STATE.catalog is None


def test_nested_prefix_rejected() -> None:
    first = _install(
        provider_api.ProviderBinding(
            prefix="testp-",
            display_name="TestProvider",
            key_env="TESTP_API_KEY",
            build=lambda _ctx: FakeListChatModel(responses=["x"]),
        ),
        models={},
        pricing={},
    )
    with pytest.raises(ValueError, match="nests inside"):
        _install(
            builder=first,
            binding=provider_api.ProviderBinding(
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


def test_plugin_model_id_must_match_binding_prefix() -> None:
    with pytest.raises(ValueError, match="must start with"):
        _install(
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
        _install(
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


def test_spawnable_model_without_price_rejected(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(with_price=False)
    with pytest.raises(provider_api.ProviderRegistrationError, match="no current price") as excinfo:
        model_catalog()
    assert "no current price" in str(excinfo.value.__cause__)


def test_model_validation_failure_leaves_the_builder_retryable() -> None:
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
    builder = CatalogBuilder()

    with pytest.raises(provider_api.ProviderRegistrationError, match="missing registry facts"):
        _install(binding, models={"testp-1": invalid}, pricing={"testp-1": price}, builder=builder)

    assert not builder.has_bindings

    valid = ModelSpec(
        provider="testp",
        spawnable=True,
        context_window=200_000,
        knowledge_cutoff="2026-01",
        effort_levels=("low", "high"),
        tuning=ModelTuning(reasoning_effort="high"),
    )
    _install(binding, models={"testp-1": valid}, pricing={"testp-1": price}, builder=builder)
    catalog = builder.build()

    assert "testp-1" in catalog.prices.plugin
    assert catalog.models["testp-1"] == valid
    assert catalog.bindings["testp-"] == binding


def test_loader_revalidates_cross_model_constraints(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(superseded_by="testp-missing")

    with pytest.raises(RuntimeError, match="not in models"):
        model_catalog()

    assert plugin_loader._STATE.catalog is None


@pytest.mark.parametrize(
    ("cache_miss", "source_checked_at", "error"),
    [
        (math.nan, "2026-08-22", "finite and non-negative"),
        (1.0, "2026-8-22", "source_checked_at"),
    ],
)
def test_plugin_price_validation(cache_miss: float, source_checked_at: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        pricing.plugin_model_price(
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
    model_catalog()
    from base.lm.stop import classify_stop

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
    model_catalog()
    assert "testp-1" not in model_catalog().models
    assert "kept-1" in model_catalog().models


def test_broken_provider_plugin_skipped_and_others_load(
    provider_plugin: Callable[..., None], loguru_records: list[dict]
) -> None:
    """Fail-soft (user ruling 2026-09-11): a provider.py that raises at import
    is skipped with a loud report; the remaining providers still register —
    one broken plugin must not take down every process that builds a model
    (agent, gateway, labeler, eval)."""
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    broken = paths.plugins_dir() / "broken_provider"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "plugin.py").write_text("# broken provider stub")
    (broken / "provider.py").write_text("raise RuntimeError('provider boom')\n")
    try:
        model_catalog()  # must not raise

        assert "kept-1" in model_catalog().models
        assert any(
            "broken_provider" in r["message"] and "failed to load" in r["message"]
            for r in loguru_records
        )
    finally:
        shutil.rmtree(broken, ignore_errors=True)


def test_provider_missing_provider_py_is_noop(provider_plugin: Callable[..., None]) -> None:
    # A plugin dir with only plugin.py (the common case) registers nothing.
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    plugin_dir = paths.plugins_dir() / "no_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text("# empty")
    model_catalog()
    assert "kept-1" in model_catalog().models
    assert "no_provider-1" not in model_catalog().models


def test_provider_only_dir_is_not_a_plugin(provider_plugin: Callable[..., None]) -> None:
    # A dir with provider.py but no plugin.py is not discovered — discovery
    # identity is plugin.py, documented in provider_api.
    provider_plugin(prefix="kept-", model="kept-1", dir_name="enabled_provider")
    plugin_dir = paths.plugins_dir() / "orphan_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "provider.py").write_text("# no plugin.py beside me")
    model_catalog()
    assert "kept-1" in model_catalog().models
    assert "orphan-provider-1" not in model_catalog().models
