"""Repository Anthropic and OpenAI model catalog contracts."""

from __future__ import annotations

from shared import plugins_config
from shared.lm import pricing, provider_api, stop
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.factory import SUPPORTED_MODELS
from shared.lm.registry import MODELS


def test_repo_anthropic_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_anthropic"].enabled
    ensure_provider_plugins_loaded()

    claude_models = {
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-fable-5",
        "claude-fable-5-1",
    }
    assert claude_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["claude"]) == {
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
        "claude-opus-5-5",
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


def test_claude_successors_and_thinking_contract() -> None:
    ensure_provider_plugins_loaded()
    opus = MODELS["claude-opus-5-5"]
    assert opus.spawnable and opus.context_window == 1_000_000
    assert opus.max_output_tokens == 128_000
    assert opus.knowledge_cutoff == "2026-06"
    assert opus.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert opus.tuning.reasoning_effort == "medium"
    assert opus.media_types == frozenset({"image", "pdf"})
    assert MODELS["claude-opus-5"].superseded_by == "claude-opus-5-5"
    assert MODELS["claude-opus-5"].spawnable
    for model in ("claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"):
        assert MODELS[model].thinking_always_on, model
    for model in ("claude-sonnet-5", "claude-opus-5"):
        assert not MODELS[model].thinking_always_on, model


def test_gpt6_sol_luna_successors_and_capabilities() -> None:
    ensure_provider_plugins_loaded()
    assert MODELS["gpt-5.6-sol"].superseded_by == "gpt-6-sol"
    assert MODELS["gpt-5.6-luna"].superseded_by == "gpt-6-luna"
    assert MODELS["gpt-5.6-sol"].spawnable and MODELS["gpt-5.6-luna"].spawnable
    assert MODELS["gpt-5.6-terra"].superseded_by is None
    for model, cutoff in (("gpt-6-sol", "2026-04"), ("gpt-6-luna", "2026-05")):
        spec = MODELS[model]
        assert spec.spawnable and spec.context_window == 1_050_000
        assert spec.max_output_tokens is None  # GPT provider leaves the API cap unpinned.
        assert spec.knowledge_cutoff == cutoff
        assert spec.effort_levels == ("none", "low", "medium", "high", "xhigh", "max")
        assert spec.tuning.reasoning_effort == "medium"
        assert spec.media_types == frozenset({"image"})


def test_repo_openai_provider_is_enabled_and_registers_complete_contract() -> None:
    discovered = plugins_config._discover_plugins()
    config = plugins_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_openai"].enabled
    ensure_provider_plugins_loaded()

    gpt_models = {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-sol",
        "gpt-6-luna",
    }
    assert gpt_models <= MODELS.keys()
    assert set(SUPPORTED_MODELS["gpt"]) == {
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-sol",
        "gpt-6-luna",
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
