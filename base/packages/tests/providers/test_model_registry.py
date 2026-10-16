from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from base.config import field_names, get_field
from base.lm.catalog import ModelCatalog
from base.lm.registry import (
    DEFAULT_TUNING,
    ModelSpec,
    ModelTuning,
    resolve_available_model,
    resolve_setting,
)
from base.packages.plugin_providers import build_model_catalog


def _validate_with(overrides: dict[str, ModelSpec]) -> None:
    from base.lm import registry as reg

    catalog = build_model_catalog()
    reg.validate_models({**catalog.models, **overrides}, prices=catalog.prices)


def test_tuning_field_names_are_real_config_fields() -> None:
    config_fields = field_names()
    for f in dataclass_fields(ModelTuning):
        assert f.name in config_fields, (
            f"ModelTuning.{f.name} has no matching config field — rename it in "
            f"base/lm/registry.py to track the settings field"
        )


def test_default_tuning_is_fully_populated() -> None:
    for f in dataclass_fields(ModelTuning):
        assert getattr(DEFAULT_TUNING, f.name) is not None, f.name


def test_stream_total_timeout_resolves_shared_floor_and_explicit_override(
    *, model_catalog: ModelCatalog
) -> None:
    assert (
        resolve_setting(
            "llm_stream_total_timeout_seconds",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=None,
        )
        == 3600.0
    )

    assert (
        resolve_setting(
            "llm_stream_total_timeout_seconds",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=7200.0,
        )
        == 7200.0
    )


def test_deepseek_stall_wave_ttft_default_is_150(*, model_catalog: ModelCatalog) -> None:
    assert (
        resolve_setting(
            "llm_stream_ttft_timeout_seconds",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=None,
        )
        == 150.0
    )

    assert (
        resolve_setting(
            "llm_stream_ttft_timeout_seconds",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=90.0,
        )
        == 90.0
    )


def test_sentinelized_config_fields_default_to_none() -> None:
    from pydantic_core import PydanticUndefined

    from base.config import FIELD_INFOS

    for f in dataclass_fields(ModelTuning):
        default = FIELD_INFOS[f.name].default
        assert default is None and default is not PydanticUndefined, (
            f"config field {f.name!r} default is {default!r}, expected the None "
            f"sentinel (its shared default lives on DEFAULT_TUNING)"
        )


def test_every_spawnable_model_has_core_facts(*, model_catalog: ModelCatalog) -> None:
    from base.lm.pricing import rates_at

    for provider, model_list in model_catalog.supported_models.items():
        for model in model_list:
            spec = model_catalog.models[model]
            assert spec.provider == provider
            assert spec.spawnable
            assert spec.context_window is not None
            assert spec.knowledge_cutoff is not None
            assert spec.effort_levels is not None
            assert rates_at(model, input_tokens=0, prices=model_catalog.prices) is not None


def test_superseded_models_stay_spawnable(*, model_catalog: ModelCatalog) -> None:
    for model_id, spec in model_catalog.models.items():
        if spec.superseded_by is None:
            continue
        assert spec.spawnable, model_id
        assert spec.superseded_by in model_catalog.models, model_id


def test_gemini_3_8_flash_is_spawnable_again(*, model_catalog: ModelCatalog) -> None:
    assert "gemini-3.8-flash" in model_catalog.supported_models["gemini"]
    assert "gemini-3.7-flash" in model_catalog.supported_models["gemini"]
    assert (
        resolve_available_model("gemini-3.8-flash", models=model_catalog.models)
        == "gemini-3.8-flash"
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
def test_retired_model_is_absent_from_registry(model: str, *, model_catalog: ModelCatalog) -> None:
    assert model not in model_catalog.models
    assert all(model not in models for models in model_catalog.supported_models.values())
    assert resolve_available_model(model, models=model_catalog.models) == model


def test_deepseek_flash_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["deepseek-flash"]
    assert spec.provider == "deepseek"
    assert spec.spawnable
    assert spec.context_window == 1_000_000
    assert spec.max_output_tokens == 384_000
    assert spec.knowledge_cutoff == "2026-04"
    assert spec.model_identity == "You are running on DeepSeek Flash."
    assert spec.effort_levels == ("high", "max")
    assert "deepseek-flash" in model_catalog.supported_models["deepseek"]
    assert (
        resolve_setting(
            "reasoning_effort",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "max"
    )
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("auto_compact_fraction"),
        )
        == 0.512
    )
    assert (
        resolve_setting(
            "compact_reminder_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("compact_reminder_fraction"),
        )
        == 0.374
    )


def test_gemini_flash_lite_latest_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["gemini-flash-lite-latest"]
    assert spec.provider == "gemini"
    assert spec.spawnable
    assert spec.context_window == 1_048_576
    assert spec.knowledge_cutoff == "2026-03"
    assert spec.effort_levels == ("minimal", "low", "medium", "high")
    assert spec.media_types == frozenset({"image", "pdf", "audio", "video"})
    assert (
        resolve_setting(
            "reasoning_effort",
            model="gemini-flash-lite-latest",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "minimal"
    )


def test_gemini_4_argon_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["gemini-4-argon"]
    assert spec.provider == "gemini"
    assert spec.spawnable
    assert spec.context_window == 1_048_576
    assert spec.knowledge_cutoff == "2026-03"
    assert spec.effort_levels == ("low", "medium", "high")
    assert spec.media_types == frozenset({"image", "pdf", "audio", "video"})
    assert (
        resolve_setting(
            "reasoning_effort",
            model="gemini-4-argon",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "medium"
    )


def test_superseded_chain_validation_rejects_self_link(*, model_catalog: ModelCatalog) -> None:
    from dataclasses import replace

    models = model_catalog.models
    with pytest.raises(RuntimeError, match="its own replacement"):
        _validate_with({"glm-5.2": replace(models["glm-5.2"], superseded_by="glm-5.2")})


def test_superseded_chain_validation_rejects_unknown_target(*, model_catalog: ModelCatalog) -> None:
    from dataclasses import replace

    models = model_catalog.models
    with pytest.raises(RuntimeError, match="not in models"):
        _validate_with({"glm-5.2": replace(models["glm-5.2"], superseded_by="glm-9.9")})


def test_superseded_chain_validation_rejects_non_spawnable_target(
    *, model_catalog: ModelCatalog
) -> None:
    from dataclasses import replace

    models = model_catalog.models
    with pytest.raises(RuntimeError, match="not spawnable"):
        _validate_with(
            {
                "gpt-5.6-sol": replace(models["gpt-5.6-sol"], spawnable=False),
                "glm-5.2": replace(models["glm-5.2"], superseded_by="gpt-5.6-sol"),
            }
        )


def test_superseded_chain_validation_rejects_cycle(*, model_catalog: ModelCatalog) -> None:
    from dataclasses import replace

    models = model_catalog.models
    with pytest.raises(RuntimeError, match="cycle"):
        _validate_with(
            {
                "glm-5.2": replace(models["glm-5.2"], superseded_by="kimi-k3"),
                "kimi-k3": replace(models["kimi-k3"], superseded_by="glm-5.2"),
            }
        )


def test_superseded_chain_validation_accepts_valid_link(*, model_catalog: ModelCatalog) -> None:
    from dataclasses import replace

    models = model_catalog.models
    _validate_with({"glm-5.2": replace(models["glm-5.2"], superseded_by="kimi-k3")})


def test_glm_5_3_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["glm-5.3"]
    assert spec.provider == "glm"
    assert spec.spawnable
    assert spec.context_window == 1_000_000
    assert spec.knowledge_cutoff == "2025-12"
    assert spec.effort_levels == ("low", "high", "max")
    assert spec.media_types == frozenset()
    assert (
        resolve_setting(
            "reasoning_effort",
            model="glm-5.3",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "max"
    )
    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="glm-5.3",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 10
    )


def test_glm_5_3_flash_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["glm-5.3-flash"]
    assert spec.provider == "glm"
    assert spec.spawnable
    assert spec.context_window == 1_000_000
    assert spec.knowledge_cutoff == "2025-12"
    assert spec.effort_levels == ("low", "high", "max")
    assert spec.media_types == frozenset({"image"})
    assert (
        resolve_setting(
            "reasoning_effort",
            model="glm-5.3-flash",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "max"
    )
    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="glm-5.3-flash",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 10
    )


def test_glm_5_3_flashx_registry_facts(*, model_catalog: ModelCatalog) -> None:
    spec = model_catalog.models["glm-5.3-flashx"]
    assert spec.provider == "glm"
    assert spec.spawnable
    assert spec.context_window == 1_000_000
    assert spec.knowledge_cutoff == "2025-12"
    assert spec.effort_levels == ("low", "high", "max")
    assert spec.media_types == frozenset({"image"})
    assert (
        resolve_setting(
            "reasoning_effort",
            model="glm-5.3-flashx",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == "max"
    )
    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="glm-5.3-flashx",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 10
    )


def test_mimo_v2_6_registry_facts(*, model_catalog: ModelCatalog) -> None:
    for model, spec in (
        ("mimo-v2.6-pro", model_catalog.models["mimo-v2.6-pro"]),
        ("mimo-v2.6-pro-ultraspeed", model_catalog.models["mimo-v2.6-pro-ultraspeed"]),
    ):
        assert spec.provider == "mimo"
        assert spec.spawnable
        assert spec.context_window == 1_000_000
        assert spec.max_output_tokens == 128_000
        assert spec.knowledge_cutoff == "2024-12"
        assert spec.effort_levels == ("none", "high")
        assert (
            resolve_setting(
                "reasoning_effort",
                model=model,
                models=model_catalog.models,
                explicit=get_field("reasoning_effort"),
            )
            == "high"
        )

    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="mimo-v2.6-pro",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 6
    )
    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="mimo-v2.6-pro-ultraspeed",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 10
    )


def test_glm_5_3_series_thinking_cannot_be_disabled(*, model_catalog: ModelCatalog) -> None:
    assert model_catalog.models["glm-5.3"].thinking_always_on
    assert model_catalog.models["glm-5.3-flash"].thinking_always_on
    assert model_catalog.models["glm-5.3-flashx"].thinking_always_on
    assert not model_catalog.models["glm-5.2"].thinking_always_on


def test_image_media_types_match_the_verified_model_matrix(*, model_catalog: ModelCatalog) -> None:
    expected = {
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
        "gemini-4-argon",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-flash-lite-latest",
        "gemini-3.1-pro-preview",
        "gpt-6-astra",
        "gpt-6-astra-fast",
        "gpt-6-sol",
        "gpt-6-sol-fast",
        "gpt-6.1-sol",
        "gpt-6.1-sol-fast",
        "gpt-6-luna",
        "gpt-6-luna-fast",
        "gpt-5.6-sol",
        "gpt-5.6-sol-fast",
        "gpt-5.6-terra",
        "gpt-5.6-terra-fast",
        "gpt-5.6-luna",
        "gpt-5.6-luna-fast",
        "kimi-k3",
        "deepseek-flash",
        "glm-5.3-flash",
        "glm-5.3-flashx",
        "qwen3.8-max",
        "qwen3.8-27b",
        "qwen3.8-flash",
    }
    assert {
        model for model, spec in model_catalog.models.items() if "image" in spec.media_types
    } == expected


def test_qwen_roster_is_exactly_the_three_flat_tier_models(*, model_catalog: ModelCatalog) -> None:
    assert sorted(model_catalog.supported_models["qwen"]) == [
        "qwen3.8-27b",
        "qwen3.8-flash",
        "qwen3.8-max",
    ]


def test_model_ids_match_their_provider_prefix(*, model_catalog: ModelCatalog) -> None:
    for model, spec in model_catalog.models.items():
        assert model.startswith(spec.provider), (model, spec.provider)


def test_user_tone_defaults_are_per_family(*, model_catalog: ModelCatalog) -> None:
    assert DEFAULT_TUNING.prompt_user_tone_enabled is True
    for model, spec in model_catalog.models.items():
        expected = False if spec.provider == "claude" else None
        assert spec.tuning.prompt_user_tone_enabled is expected, model
