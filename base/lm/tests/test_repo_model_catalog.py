"""Repository Anthropic and OpenAI model catalog contracts."""

from __future__ import annotations

from base.lm import pricing, stop
from base.lm.catalog import ModelCatalog
from base.packages.plugins import enable_config


def test_repo_anthropic_provider_is_enabled_and_registers_complete_contract(
    *, model_catalog: ModelCatalog
) -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_anthropic"].enabled

    claude_models = {
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5-20251001",
        "claude-haiku-5-5",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-fable-5",
        "claude-fable-5-1",
    }
    assert claude_models <= model_catalog.models.keys()
    assert set(model_catalog.supported_models["claude"]) == {
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5-20251001",
        "claude-haiku-5-5",
        "claude-opus-5",
        "claude-opus-5-fast",
        "claude-opus-5-5",
        "claude-opus-5-5-fast",
        "claude-fable-5",
        "claude-fable-5-1",
    }
    assert pricing.model_vendor("claude-sonnet-5", prices=model_catalog.prices) == "anthropic"

    from base.lm.factory import provider_key_map

    assert provider_key_map(catalog=model_catalog)["claude-"] == ("Anthropic", "ANTHROPIC_API_KEY")
    binding = model_catalog.bindings["claude-"]
    assert binding.effort_levels is None
    assert binding.anthropic_protocol
    assert binding.vision
    assert binding.stop_spec == stop.StopSpec(
        "anthropic",
        "stop_reason",
        frozenset({"end_turn", "tool_use", "refusal"}),
        frozenset({"max_tokens"}),
    )


def test_claude_successors_and_thinking_contract(*, model_catalog: ModelCatalog) -> None:
    opus = model_catalog.models["claude-opus-5-5"]
    assert opus.spawnable and opus.context_window == 1_000_000
    assert opus.max_output_tokens == 128_000
    assert opus.knowledge_cutoff == "2026-06"
    assert opus.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert opus.tuning.reasoning_effort == "medium"
    assert opus.media_types == frozenset({"image", "pdf"})
    assert model_catalog.models["claude-opus-5"].superseded_by == "claude-opus-5-5"
    assert model_catalog.models["claude-opus-5"].spawnable
    for model in ("claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"):
        assert model_catalog.models[model].thinking_always_on, model
    for model in ("claude-sonnet-5", "claude-opus-5"):
        assert not model_catalog.models[model].thinking_always_on, model


def test_claude_sonnet_5_5_capabilities_and_display_successor(
    *, model_catalog: ModelCatalog
) -> None:
    sonnet = model_catalog.models["claude-sonnet-5-5"]
    assert sonnet.spawnable and sonnet.context_window == 1_000_000
    assert sonnet.max_output_tokens == 128_000
    assert sonnet.knowledge_cutoff == "2026-06"
    assert sonnet.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert sonnet.tuning.reasoning_effort == "high"
    assert sonnet.media_types == frozenset({"image", "pdf"})
    assert sonnet.thinking_always_on
    assert model_catalog.models["claude-sonnet-5"].superseded_by == "claude-sonnet-5-5"
    assert model_catalog.models["claude-sonnet-5"].spawnable


def test_gpt6_sol_luna_successors_and_capabilities(*, model_catalog: ModelCatalog) -> None:
    assert model_catalog.models["gpt-5.6-sol"].superseded_by == "gpt-6-sol"
    assert model_catalog.models["gpt-5.6-luna"].superseded_by == "gpt-6-luna"
    assert (
        model_catalog.models["gpt-5.6-sol"].spawnable
        and model_catalog.models["gpt-5.6-luna"].spawnable
    )
    assert model_catalog.models["gpt-5.6-terra"].superseded_by is None
    for model, cutoff in (("gpt-6-sol", "2026-04"), ("gpt-6-luna", "2026-05")):
        spec = model_catalog.models[model]
        assert spec.spawnable and spec.context_window == 1_050_000
        assert spec.max_output_tokens is None  # GPT provider leaves the API cap unpinned.
        assert spec.knowledge_cutoff == cutoff
        assert spec.effort_levels == ("none", "low", "medium", "high", "xhigh", "max")
        assert spec.tuning.reasoning_effort == "medium"
        assert spec.media_types == frozenset({"image"})


def test_gpt6_1_sol_capabilities_and_display_successor(*, model_catalog: ModelCatalog) -> None:
    sol_61 = model_catalog.models["gpt-6.1-sol"]
    assert sol_61.spawnable and sol_61.context_window == 1_050_000
    assert sol_61.max_output_tokens is None
    assert sol_61.knowledge_cutoff == "2026-04"
    assert sol_61.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert sol_61.tuning.reasoning_effort == "medium"
    assert sol_61.media_types == frozenset({"image"})
    assert model_catalog.models["gpt-6-sol"].superseded_by == "gpt-6.1-sol"
    assert model_catalog.models["gpt-6-sol"].spawnable


def test_repo_openai_provider_is_enabled_and_registers_complete_contract(
    *, model_catalog: ModelCatalog
) -> None:
    discovered = enable_config.discover_plugins()
    config = enable_config.load_for_runtime(set(discovered))

    assert config.plugins["lm_openai"].enabled

    gpt_models = {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-sol",
        "gpt-6.1-sol",
        "gpt-6-luna",
    }
    assert gpt_models <= model_catalog.models.keys()
    assert set(model_catalog.supported_models["gpt"]) == {
        "gpt-6-astra",
        "gpt-6-astra-fast",
        "gpt-5.6-sol",
        "gpt-5.6-sol-fast",
        "gpt-5.6-terra",
        "gpt-5.6-terra-fast",
        "gpt-5.6-luna",
        "gpt-5.6-luna-fast",
        "gpt-6-sol",
        "gpt-6-sol-fast",
        "gpt-6.1-sol",
        "gpt-6.1-sol-fast",
        "gpt-6-luna",
        "gpt-6-luna-fast",
    }
    assert pricing.model_vendor("gpt-5.6-sol", prices=model_catalog.prices) == "openai"

    from base.lm.factory import provider_key_map

    assert provider_key_map(catalog=model_catalog)["gpt-"] == ("OpenAI", "OPENAI_API_KEY")
    binding = model_catalog.bindings["gpt-"]
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


def test_haiku_5_5_adaptive_capabilities_preserve_manual_predecessor(
    *, model_catalog: ModelCatalog
) -> None:
    catalog = model_catalog
    new = catalog.models["claude-haiku-5-5"]
    assert new.spawnable and new.context_window == 1_000_000
    assert new.max_output_tokens == 128_000
    assert new.knowledge_cutoff == "2026-06"
    assert new.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert new.tuning.reasoning_effort == "medium"
    assert not new.thinking_always_on and not new.extended_thinking_only
    assert new.media_types == frozenset({"image", "pdf"})
    old = catalog.models["claude-haiku-4-5-20251001"]
    assert old.superseded_by == "claude-haiku-5-5" and old.spawnable
    assert old.extended_thinking_only and old.effort_levels == ("none", "high")
