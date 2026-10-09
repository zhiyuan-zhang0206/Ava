"""Tests for `base/lm/registry.py` — the single per-model table (facts +
tuning defaults) and the `resolve_setting` config layering.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import fields as dataclass_fields
from pathlib import Path

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
from tests.fixtures.model_catalog import AddModels

# ---------------------------------------------------------------------------
# resolve_setting layering
# ---------------------------------------------------------------------------


def test_shared_floor_applies_when_nothing_set(*, model_catalog: ModelCatalog) -> None:
    # claude-sonnet-5 carries no compact opinions of its own — the shared floor.
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
    """User decision (2026-08-29): the deepseek entry compacts at soft
    374k / hard 512k on its 1M window — 0.374 / 0.512 of the window."""
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
    """An unknown model simply has no per-model layer — the shared floor (or
    an explicit value) still resolves."""
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
    # A field the entry has no opinion on still falls to the shared floor.
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
    monkeypatch: pytest.MonkeyPatch, add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    """A non-None settings value (env/.env/per-agent overlay all write one) is
    the explicit layer — it beats the per-model default."""
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
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.5)
    assert (
        resolve_setting(
            "auto_compact_fraction",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("auto_compact_fraction"),
        )
        == 0.5
    )


def test_explicit_empty_string_beats_per_model_effort(
    monkeypatch: pytest.MonkeyPatch, add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    """An explicitly empty AVA_REASONING_EFFORT is a real choice ("use the
    provider default"), distinct from unset — it must mask a per-model effort
    default rather than fall through it."""
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
    monkeypatch.setattr(settings.lm, "reasoning_effort", "")
    assert (
        resolve_setting(
            "reasoning_effort",
            model="deepseek-flash",
            models=model_catalog.models,
            explicit=get_field("reasoning_effort"),
        )
        == ""
    )


def test_unknown_setting_fails_fast(*, model_catalog: ModelCatalog) -> None:
    """A name that is not a ModelTuning field raises instead of silently
    resolving to something — both a typo and a real-but-non-per-model config
    field (the membership check runs before the explicit-value shortcut)."""
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


# ---------------------------------------------------------------------------
# explain_setting — the same layering, with the winning layer named
# ---------------------------------------------------------------------------


def test_tuning_field_names_are_the_governed_set() -> None:
    """`tuning_field_names` IS ModelTuning's field list — the per-model view
    enumerates through it, so a new tunable never needs a second list."""
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
    """Every layer combination reports the value AND which layer produced it,
    while the losing candidates stay visible (the whole point of the view)."""
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
    """resolve_setting is explain_setting's `.value` — a config panel built on
    one cannot show a value the runtime doesn't use."""
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
    """Same fail-fast membership gate as resolve_setting — a real-but-not-per-model
    config field must not resolve through the per-model path."""
    with pytest.raises(AttributeError):
        explain_setting(
            "labeler_model", model="deepseek-flash", explicit=None, models=model_catalog.models
        )


def test_compact_fractions_are_per_agent_overridable() -> None:
    """The compact fractions ride the per-agent overlay (the topmost layer);
    the overlay gate is the per_agent flag on the settings field."""
    per_agent = per_agent_field_names()
    assert "auto_compact_fraction" in per_agent
    assert "compact_reminder_fraction" in per_agent
    assert "reasoning_effort" in per_agent


# ---------------------------------------------------------------------------
# Cross-profile reads (Task #944): the tuning fields live in the AGENT config
# domain, but the gateway's token-usage / context-breakdown display endpoints
# resolve them too. A profile without the agent domain must degrade to the
# registry floor, not AttributeError — the agent process itself keeps reading
# the explicit value.
# ---------------------------------------------------------------------------


def test_resolve_setting_degrades_when_owning_domain_not_in_profile(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """A display caller without the owning domain passes no explicit pin;
    registry resolution must not recover a hidden settings reader."""
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
    # the no-explicit resolution (model layer over the shared floor), never the
    # sentinel and never a crash
    expected = explain_setting(
        "auto_compact_fraction", model="deepseek-flash", explicit=None, models=model_catalog.models
    ).value
    assert value == expected


def test_resolve_setting_still_reads_explicit_value_in_full_profile(
    *, model_catalog: ModelCatalog
) -> None:
    """In a full (profile-less) process — the agent's own — the explicit value
    still wins: the degradation must not leak into the owner process."""
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


# ---------------------------------------------------------------------------
# attach modalities
# ---------------------------------------------------------------------------


def test_attach_modalities_default_to_the_declared_media_matrix(
    *, model_catalog: ModelCatalog
) -> None:
    """attach_modalities is an override, not a second matrix: a model with no
    attach-specific opinion attaches exactly its registry media_types, and a
    text-only model attaches nothing (user ruling 2026-08-28)."""
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
    assert (
        attach_modalities_for_model(
            "deepseek-flash",
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
    """An attach_modalities declaration outside the model's media_types is a
    registry error — attach rides the same message pipeline (user ruling
    2026-08-28)."""
    from dataclasses import replace

    from base.lm import registry as reg

    bad = replace(model_catalog.models["glm-5.3-flash"], attach_modalities=frozenset({"video"}))
    with pytest.raises(RuntimeError, match="attach_modalities"):
        reg.validate_spec(
            "glm-5.3-flash", bad, anthropic_protocol=False, prices=model_catalog.prices
        )
    # A strict subset (attach narrower than the endpoint) is legal.
    narrower = replace(
        model_catalog.models["gemini-3.8-flash"], attach_modalities=frozenset({"image"})
    )
    reg.validate_spec(
        "gemini-3.8-flash", narrower, anthropic_protocol=False, prices=model_catalog.prices
    )


def test_reasoning_effort_default_must_stay_within_effort_levels(
    *, model_catalog: ModelCatalog
) -> None:
    """A spawnable model whose pinned default is not one of its effort_levels
    would render no selected rung in the spawn picker while a different effort
    goes on the wire — the same what-you-see != what-is-sent class as a
    missing default."""
    from dataclasses import replace

    from base.lm import registry as reg

    spec = model_catalog.models["glm-5.3-flash"]
    bad = replace(spec, tuning=replace(spec.tuning, reasoning_effort="ultra"))
    with pytest.raises(RuntimeError, match="outside its effort_levels"):
        reg.validate_spec(
            "glm-5.3-flash", bad, anthropic_protocol=False, prices=model_catalog.prices
        )


def test_explicit_catalog_resolves_withdrawal_in_a_fresh_process() -> None:
    """The composition root builds a catalog; synthetic changes stay local."""
    code = textwrap.dedent(
        """
        from dataclasses import replace
        from base.lm import plugin_providers
        from base.lm.registry import resolve_available_model

        assert not hasattr(plugin_providers, "_STATE")
        catalog = plugin_providers.build_model_catalog()
        assert resolve_available_model("deepseek-flash", models=catalog.models) == "deepseek-flash"
        withdrawn = replace(
            catalog.models["deepseek-flash"], spawnable=False,
            unavailable_fallback="deepseek-flash",
        )
        changed = replace(catalog, models={**catalog.models, "deepseek-retired-fixture": withdrawn})
        assert resolve_available_model("deepseek-retired-fixture", models=changed.models) == "deepseek-flash"
        assert "deepseek-retired-fixture" not in catalog.models
        print("OK")
        """
    )
    result = subprocess.run(  # noqa: S603 — our own venv python + a literal script
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_normalize_overlay_settles_withdrawn_model_and_returns_receipt(
    add_models: AddModels, *, model_catalog: ModelCatalog
) -> None:
    """A synthetic withdrawal keeps the write-side settlement contract covered."""
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
    """Nothing to settle for: no llm_model key, an available id, or an unknown
    id (unknown ids are `validate_config_overlay` / `validate_model_config`'s
    rejection concern — this helper must not invent a fallback for them)."""
    cases: list[dict[str, object]] = [
        {},
        {"llm_model": "deepseek-flash"},
        {"llm_model": "not-a-real-model"},
    ]
    for config in cases:
        before = dict(config)
        assert normalize_overlay_llm_model(config, models=model_catalog.models) is None
        assert config == before
