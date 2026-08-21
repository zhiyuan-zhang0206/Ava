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
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from shared import paths
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
        )"""

_PRICE_LINE = """\"{model}\": PriceRates(
            cache_miss=1.0, cache_hit=0.1, output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at="2026-08-22",
        )"""


@pytest.fixture
def provider_plugin() -> Generator[Callable[..., None], None, None]:
    """Write a fixture provider.py + enable config, then restore all
    module-level registration state after the test."""

    models_snapshot = dict(MODELS)
    bindings_snapshot = dict(provider_api.REGISTRY.bindings)
    prices_snapshot = dict(pricing._PLUGIN_PRICES)
    stop_snapshot = dict(stop._BY_PROVIDER)
    # Tests share one session AVA_HOME — remove anything this test created so
    # a later test's loader scan cannot see leftover plugin dirs.
    created: list[Path] = []

    def _write(
        prefix: str = "testp-",
        display: str = "TestProvider",
        key_env: str = "TESTP_API_KEY",
        vision: bool = False,
        stop_spec: str | None = None,
        model: str | None = "testp-1",
        with_price: bool = True,
        dir_name: str = "test_provider",
    ) -> None:
        plugin_dir = paths.plugins_dir() / dir_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        created.append(plugin_dir)
        models = _MODEL_LINE.format(model=model, provider=prefix.rstrip("-")) if model else ""
        pricing_line = _PRICE_LINE.format(model=model) if model and with_price else ""
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
    _invalidate_known_provider_keys_cache()


def test_plugin_model_registers_and_builds(provider_plugin: Callable[..., None]) -> None:
    provider_plugin()
    ensure_provider_plugins_loaded()

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
    # dispatches (matching the gemini/gpt core branches): the builder receives
    # spec=None and decides its own posture.
    llm2 = build_chat_model("testp-2")
    assert isinstance(llm2, FakeListChatModel)


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
    assert model_supports_vision("testv-1")
    assert not model_supports_vision("testp-9")


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
    with pytest.raises(ValueError, match="already claimed"):
        provider_api.register(
            provider_api.ProviderBinding(
                prefix="claude-",
                display_name="Shadow",
                key_env="X",
                build=lambda _ctx: FakeListChatModel(responses=["x"]),
            ),
            models={},
            pricing={},
        )


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
        register_models("testp", {"deepseek-v4-pro": ModelSpec(provider="testp")})


def test_spawnable_model_without_price_rejected(provider_plugin: Callable[..., None]) -> None:
    provider_plugin(with_price=False)
    with pytest.raises(RuntimeError) as excinfo:
        ensure_provider_plugins_loaded()
    assert "no current price" in str(excinfo.value.__cause__)


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
    provider_plugin()
    cfg_path = paths.ava_home() / "plugins_config.json"
    cfg_path.write_text(json.dumps({"plugins": {"test_provider": {"enabled": False}}}))
    ensure_provider_plugins_loaded()
    assert "testp-1" not in MODELS


def test_provider_missing_provider_py_is_noop(provider_plugin: Callable[..., None]) -> None:
    # A plugin dir with only plugin.py (the common case) registers nothing.
    plugin_dir = paths.plugins_dir() / "no_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text("# empty")
    ensure_provider_plugins_loaded()
    assert "testp-1" not in MODELS


def test_provider_only_dir_is_not_a_plugin(provider_plugin: Callable[..., None]) -> None:
    # A dir with provider.py but no plugin.py is not discovered — discovery
    # identity is plugin.py, documented in provider_api.
    plugin_dir = paths.plugins_dir() / "orphan_provider"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "provider.py").write_text("# no plugin.py beside me")
    ensure_provider_plugins_loaded()
    assert "testp-1" not in MODELS
