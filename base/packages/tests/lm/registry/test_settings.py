from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from base.config import get_field, per_agent_field_names, settings
from base.lm.catalog import ModelCatalog
from base.lm.registry import (
    ModelSpec,
    ModelTuning,
    explain_setting,
    normalize_overlay_llm_model,
    resolve_setting,
    tuning_field_names,
)
from base.packages.tests.model_catalog import AddModels


def test_shared_floor_applies_when_nothing_set(*, model_catalog: ModelCatalog) -> None:
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="claude-sonnet-5",
            models=model_catalog.models,
            explicit=get_field("auto_compact_fraction"),
        )
        == 0.4
    )
    assert (
        resolve_setting(
            "compact_reminder_fraction",
            model="claude-sonnet-5",
            models=model_catalog.models,
            explicit=get_field("compact_reminder_fraction"),
        )
        == 0.3
    )
    assert (
        resolve_setting(
            "llm_retry_max_attempts",
            model="claude-sonnet-5",
            models=model_catalog.models,
            explicit=get_field("llm_retry_max_attempts"),
        )
        == 6
    )
    assert (
        resolve_setting(
            "agent_communication_style",
            model="gpt-5.6-sol",
            models=model_catalog.models,
            explicit=get_field("agent_communication_style"),
        )
        == "off"
    )
    assert (
        resolve_setting(
            "prompt_temporal_awareness_enabled",
            model="glm-5.2",
            models=model_catalog.models,
            explicit=get_field("prompt_temporal_awareness_enabled"),
        )
        is True
    )


def test_deepseek_carries_per_model_compact_thresholds(*, model_catalog: ModelCatalog) -> None:
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


def test_unregistered_model_falls_back_to_shared_floor(*, model_catalog: ModelCatalog) -> None:
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="no-such-model",
            models=model_catalog.models,
            explicit=get_field("auto_compact_fraction"),
        )
        == 0.4
    )


def test_per_model_default_wins_over_shared_floor(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    spec = model_catalog.models["deepseek-flash"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(auto_compact_fraction=0.9, agent_communication_style="silent"),
    )
    model_catalog = add_models(model_catalog, {"deepseek-flash": tuned})
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("auto_compact_fraction"),
        )
        == 0.9
    )
    assert (
        resolve_setting(
            "agent_communication_style",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("agent_communication_style"),
        )
        == "silent"
    )
    assert (
        resolve_setting(
            "compact_reminder_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("compact_reminder_fraction"),
        )
        == 0.3
    )


def test_explicit_setting_wins_over_per_model_default(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    spec = model_catalog.models["deepseek-flash"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(auto_compact_fraction=0.9, reasoning_effort="high"),
    )
    model_catalog = add_models(model_catalog, {"deepseek-flash": tuned})
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=0.5,
        )
        == 0.5
    )


def test_explicit_empty_string_beats_per_model_effort(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    spec = model_catalog.models["deepseek-flash"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(reasoning_effort="max"),
    )
    model_catalog = add_models(model_catalog, {"deepseek-flash": tuned})
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
            "reasoning_effort",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit="",
        )
        == ""
    )


def test_unknown_setting_fails_fast(*, model_catalog: ModelCatalog) -> None:
    with pytest.raises(AttributeError):
        resolve_setting(
            "no_such_setting",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=None,
        )
    with pytest.raises(AttributeError):
        resolve_setting(
            "labeler_model",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("labeler_model"),
        )


def test_tuning_field_names_are_the_governed_set() -> None:
    assert tuning_field_names() == tuple(f.name for f in dataclass_fields(ModelTuning))


@pytest.mark.parametrize(
    ("explicit", "tuned", "expected_source", "expected_value"),
    [
        (None, None, "shared-default", 0.4),
        (None, 0.9, "model-default", 0.9),
        (0.5, 0.9, "explicit", 0.5),
        (0.5, None, "explicit", 0.5),
    ],
)
def test_explain_setting_names_the_winning_layer(
    monkeypatch: pytest.MonkeyPatch,
    add_models: AddModels,
    explicit: float | None,
    tuned: float | None,
    expected_source: str,
    expected_value: float,
    *,
    model_catalog: ModelCatalog,
) -> None:
    spec = model_catalog.models["deepseek-flash"]
    model_catalog = add_models(
        model_catalog,
        {
            "deepseek-flash": ModelSpec(
                provider=spec.provider,
                spawnable=spec.spawnable,
                context_window=spec.context_window,
                max_output_tokens=spec.max_output_tokens,
                knowledge_cutoff=spec.knowledge_cutoff,
                effort_levels=spec.effort_levels,
                tuning=ModelTuning(auto_compact_fraction=tuned),
            )
        },
    )
    resolved = explain_setting(
        "auto_compact_fraction",
        model="deepseek-flash",
        explicit=explicit,
        models=model_catalog.models,
    )
    assert (resolved.source, resolved.value) == (expected_source, expected_value)
    assert resolved.shared_default == 0.4
    assert resolved.model_default == tuned
    assert resolved.explicit_value == explicit


def test_explain_setting_agrees_with_resolve_setting(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setattr(settings.agent, "compact_reminder_fraction", 0.33)
    for setting in tuning_field_names():
        runtime = resolve_setting(
            setting, model="claude-opus-5", models=model_catalog.models, explicit=get_field(setting)
        )
        explained = explain_setting(
            setting, model="claude-opus-5", explicit=get_field(setting), models=model_catalog.models
        )
        assert explained.value == runtime, setting


def test_explain_setting_rejects_a_non_tuning_field(*, model_catalog: ModelCatalog) -> None:
    with pytest.raises(AttributeError):
        explain_setting(
            "labeler_model", model="deepseek-flash", explicit=None, models=model_catalog.models
        )


def test_compact_fractions_are_per_agent_overridable() -> None:
    per_agent = per_agent_field_names()
    assert "auto_compact_fraction" in per_agent
    assert "compact_reminder_fraction" in per_agent
    assert "reasoning_effort" in per_agent


def test_resolve_setting_degrades_when_owning_domain_not_in_profile(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    import base.config

    def _boom(name: str) -> object:
        raise AttributeError(
            "'gateway' process profile does not construct the 'agent' config domain"
        )

    monkeypatch.setattr(base.config, "get_field", _boom)
    value = resolve_setting(
        "auto_compact_fraction",
        model="deepseek-flash",
        models=model_catalog.models,
        explicit=None,
    )
    expected = explain_setting(
        "auto_compact_fraction", model="deepseek-flash", explicit=None, models=model_catalog.models
    ).value
    assert value == expected


def test_resolve_setting_still_reads_explicit_value_in_full_profile(
    *, model_catalog: ModelCatalog
) -> None:
    explicit = get_field("auto_compact_fraction")
    value = resolve_setting(
        "auto_compact_fraction",
        model="deepseek-flash",
        models=model_catalog.models,
        explicit=get_field("auto_compact_fraction"),
    )
    expected = explain_setting(
        "auto_compact_fraction",
        model="deepseek-flash",
        explicit=explicit,
        models=model_catalog.models,
    ).value
    assert value == expected


def test_attach_modalities_default_to_the_declared_media_matrix(
    *, model_catalog: ModelCatalog
) -> None:
    from base.lm.factory import attach_modalities_for_model

    assert attach_modalities_for_model(
        "gemini-3.8-flash",
        models=model_catalog.models,
        vision_prefixes={
            prefix: binding.vision for prefix, binding in model_catalog.bindings.items()
        },
    ) == frozenset({"image", "pdf", "audio", "video"})
    assert attach_modalities_for_model(
        "claude-sonnet-5",
        models=model_catalog.models,
        vision_prefixes={
            prefix: binding.vision for prefix, binding in model_catalog.bindings.items()
        },
    ) == frozenset({"image", "pdf"})
    assert attach_modalities_for_model(
        "glm-5.3-flash",
        models=model_catalog.models,
        vision_prefixes={
            prefix: binding.vision for prefix, binding in model_catalog.bindings.items()
        },
    ) == frozenset({"image"})
    assert attach_modalities_for_model(
        "deepseek-flash",
        models=model_catalog.models,
        vision_prefixes={
            prefix: binding.vision for prefix, binding in model_catalog.bindings.items()
        },
    ) == frozenset({"image"})
    assert (
        attach_modalities_for_model(
            "glm-5.3",
            models=model_catalog.models,
            vision_prefixes={
                prefix: binding.vision for prefix, binding in model_catalog.bindings.items()
            },
        )
        == frozenset()
    )


def test_attach_modalities_declaration_must_stay_within_media_types(
    *, model_catalog: ModelCatalog
) -> None:
    from dataclasses import replace

    from base.lm import registry as reg

    bad = replace(model_catalog.models["glm-5.3-flash"], attach_modalities=frozenset({"video"}))
    with pytest.raises(RuntimeError, match="attach_modalities"):
        reg.validate_spec(
            "glm-5.3-flash", bad, anthropic_protocol=False, prices=model_catalog.prices
        )
    narrower = replace(
        model_catalog.models["gemini-3.8-flash"], attach_modalities=frozenset({"image"})
    )
    reg.validate_spec(
        "gemini-3.8-flash", narrower, anthropic_protocol=False, prices=model_catalog.prices
    )


def test_reasoning_effort_default_must_stay_within_effort_levels(
    *, model_catalog: ModelCatalog
) -> None:
    from dataclasses import replace

    from base.lm import registry as reg

    spec = model_catalog.models["glm-5.3-flash"]
    bad = replace(spec, tuning=replace(spec.tuning, reasoning_effort="ultra"))
    with pytest.raises(RuntimeError, match="outside its effort_levels"):
        reg.validate_spec(
            "glm-5.3-flash", bad, anthropic_protocol=False, prices=model_catalog.prices
        )


def test_normalize_overlay_settles_withdrawn_model_and_returns_receipt(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    from dataclasses import replace

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
    config: dict[str, object] = {"llm_model": model, "reasoning_effort": "low"}
    assert normalize_overlay_llm_model(config, models=model_catalog.models) == (
        model,
        "deepseek-flash",
    )
    assert config == {"llm_model": "deepseek-flash", "reasoning_effort": "low"}


def test_normalize_overlay_leaves_available_unknown_and_absent_untouched(
    *, model_catalog: ModelCatalog
) -> None:
    cases: list[dict[str, object]] = [
        {},
        {"llm_model": "deepseek-flash"},
        {"llm_model": "not-a-real-model"},
    ]
    for config in cases:
        before = dict(config)
        assert normalize_overlay_llm_model(config, models=model_catalog.models) is None
        assert config == before
