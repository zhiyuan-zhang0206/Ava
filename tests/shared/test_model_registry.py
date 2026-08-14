"""Tests for `shared/lm/registry.py` — the single per-model table (facts +
tuning defaults) and the `resolve_setting` config layering.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from shared.config import field_names, get_field, per_agent_field_names, settings
from shared.lm.registry import (
    DEFAULT_TUNING,
    MODELS,
    SUPPORTED_MODELS,
    ModelSpec,
    ModelTuning,
    explain_setting,
    resolve_setting,
    tuning_field_names,
)

# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


def test_tuning_field_names_are_real_config_fields() -> None:
    """Every ModelTuning field maps 1:1 onto a flat config field name —
    resolve_setting bridges the two by name, so a settings-field rename that
    forgets the registry would silently orphan the per-model layer."""
    config_fields = field_names()
    for f in dataclass_fields(ModelTuning):
        assert f.name in config_fields, (
            f"ModelTuning.{f.name} has no matching config field — rename it in "
            f"shared/lm/registry.py to track the settings field"
        )


def test_default_tuning_is_fully_populated() -> None:
    """DEFAULT_TUNING is the resolution floor: a None there would leak the
    sentinel out of resolve_setting as an effective value."""
    for f in dataclass_fields(ModelTuning):
        assert getattr(DEFAULT_TUNING, f.name) is not None, f.name


def test_sentinelized_config_fields_default_to_none() -> None:
    """Each per-model-defaultable settings field carries the None sentinel as
    its pydantic default — a real default there would read as an explicit user
    choice and permanently mask the per-model layer."""
    from pydantic_core import PydanticUndefined

    from shared.config import FIELD_INFOS

    for f in dataclass_fields(ModelTuning):
        default = FIELD_INFOS[f.name].default
        assert default is None and default is not PydanticUndefined, (
            f"config field {f.name!r} default is {default!r}, expected the None "
            f"sentinel (its shared default lives on DEFAULT_TUNING)"
        )


def test_every_spawnable_model_has_core_facts() -> None:
    """Registry invariant (also enforced at import): a spawnable model must
    carry window, cutoff, pricing, and an effort vocabulary."""
    for provider, model_list in SUPPORTED_MODELS.items():
        for model in model_list:
            spec = MODELS[model]
            assert spec.provider == provider
            assert spec.spawnable
            assert spec.context_window is not None
            assert spec.knowledge_cutoff is not None
            assert spec.pricing is not None
            assert spec.effort_levels is not None


def test_model_ids_match_their_provider_prefix() -> None:
    """A registry entry filed under the wrong provider would dispatch to the
    wrong build_chat_model branch."""
    for model, spec in MODELS.items():
        assert model.startswith(spec.provider), (model, spec.provider)


# ---------------------------------------------------------------------------
# resolve_setting layering
# ---------------------------------------------------------------------------


def test_shared_floor_applies_when_nothing_set() -> None:
    # claude-sonnet-5 carries no compact opinions of its own — the shared floor.
    assert resolve_setting("auto_compact_fraction", model="claude-sonnet-5") == 0.4
    assert resolve_setting("compact_reminder_fraction", model="claude-sonnet-5") == 0.3
    assert resolve_setting("llm_retry_max_attempts", model="claude-sonnet-5") == 6
    assert resolve_setting("agent_communication_style", model="gpt-5.6-sol") == "oriented"
    assert resolve_setting("prompt_temporal_awareness_enabled", model="glm-5.2") is True


def test_deepseek_carries_per_model_compact_thresholds() -> None:
    """User decision (task #581): deepseek-v4-pro / deepseek-v4-flash compact
    at soft 374k / hard 512k on their 1M window — 0.374 / 0.512 of the window."""
    for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
        assert resolve_setting("auto_compact_fraction", model=model) == 0.512, model
        assert resolve_setting("compact_reminder_fraction", model=model) == 0.374, model


def test_unregistered_model_falls_back_to_shared_floor() -> None:
    """An unknown model simply has no per-model layer — the shared floor (or
    an explicit value) still resolves."""
    assert resolve_setting("auto_compact_fraction", model="no-such-model") == 0.4


def test_per_model_default_wins_over_shared_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = MODELS["deepseek-v4-pro"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        pricing=spec.pricing,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(auto_compact_fraction=0.9, agent_communication_style="silent"),
    )
    monkeypatch.setitem(MODELS, "deepseek-v4-pro", tuned)
    assert resolve_setting("auto_compact_fraction", model="deepseek-v4-pro") == 0.9
    assert resolve_setting("agent_communication_style", model="deepseek-v4-pro") == "silent"
    # A field the entry has no opinion on still falls to the shared floor.
    assert resolve_setting("compact_reminder_fraction", model="deepseek-v4-pro") == 0.3


def test_explicit_setting_wins_over_per_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-None settings value (env/.env/per-agent overlay all write one) is
    the explicit layer — it beats the per-model default."""
    spec = MODELS["deepseek-v4-pro"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        pricing=spec.pricing,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(auto_compact_fraction=0.9, reasoning_effort="high"),
    )
    monkeypatch.setitem(MODELS, "deepseek-v4-pro", tuned)
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.5)
    assert resolve_setting("auto_compact_fraction", model="deepseek-v4-pro") == 0.5


def test_explicit_empty_string_beats_per_model_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly empty AVA_REASONING_EFFORT is a real choice ("use the
    provider default"), distinct from unset — it must mask a per-model effort
    default rather than fall through it."""
    spec = MODELS["deepseek-v4-pro"]
    tuned = ModelSpec(
        provider=spec.provider,
        spawnable=spec.spawnable,
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        knowledge_cutoff=spec.knowledge_cutoff,
        pricing=spec.pricing,
        effort_levels=spec.effort_levels,
        tuning=ModelTuning(reasoning_effort="max"),
    )
    monkeypatch.setitem(MODELS, "deepseek-v4-pro", tuned)
    assert resolve_setting("reasoning_effort", model="deepseek-v4-pro") == "max"
    monkeypatch.setattr(settings.lm, "reasoning_effort", "")
    assert resolve_setting("reasoning_effort", model="deepseek-v4-pro") == ""


def test_unknown_setting_fails_fast() -> None:
    """A name that is not a ModelTuning field raises instead of silently
    resolving to something — both a typo and a real-but-non-per-model config
    field (the membership check runs before the explicit-value shortcut)."""
    with pytest.raises(AttributeError):
        resolve_setting("no_such_setting", model="deepseek-v4-pro")
    with pytest.raises(AttributeError):
        resolve_setting("labeler_model", model="deepseek-v4-pro")


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
    explicit: float | None,
    tuned: float | None,
    expected_source: str,
    expected_value: float,
) -> None:
    """Every layer combination reports the value AND which layer produced it,
    while the losing candidates stay visible (the whole point of the view)."""
    spec = MODELS["deepseek-v4-pro"]
    monkeypatch.setitem(
        MODELS,
        "deepseek-v4-pro",
        ModelSpec(
            provider=spec.provider,
            spawnable=spec.spawnable,
            context_window=spec.context_window,
            max_output_tokens=spec.max_output_tokens,
            knowledge_cutoff=spec.knowledge_cutoff,
            pricing=spec.pricing,
            effort_levels=spec.effort_levels,
            tuning=ModelTuning(auto_compact_fraction=tuned),
        ),
    )
    resolved = explain_setting("auto_compact_fraction", model="deepseek-v4-pro", explicit=explicit)
    assert (resolved.source, resolved.value) == (expected_source, expected_value)
    assert resolved.shared_default == 0.4
    assert resolved.model_default == tuned
    assert resolved.explicit_value == explicit


def test_explain_setting_agrees_with_resolve_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_setting is explain_setting's `.value` — a config panel built on
    one cannot show a value the runtime doesn't use."""
    monkeypatch.setattr(settings.agent, "compact_reminder_fraction", 0.33)
    for setting in tuning_field_names():
        runtime = resolve_setting(setting, model="claude-opus-5")
        explained = explain_setting(setting, model="claude-opus-5", explicit=get_field(setting))
        assert explained.value == runtime, setting


def test_explain_setting_rejects_a_non_tuning_field() -> None:
    """Same fail-fast membership gate as resolve_setting — a real-but-not-per-model
    config field must not resolve through the per-model path."""
    with pytest.raises(AttributeError):
        explain_setting("labeler_model", model="deepseek-v4-pro", explicit=None)


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_field raises AttributeError when the field's owning domain is not
    constructed in this process's profile (the gateway case). resolve_setting
    must fall back to the registry floor instead of propagating — a display
    read, not the agent's own tuning decision."""
    import shared.config

    def _boom(name: str) -> object:
        raise AttributeError(
            "'gateway' process profile does not construct the 'agent' config domain"
        )

    monkeypatch.setattr(shared.config, "get_field", _boom)
    value = resolve_setting("auto_compact_fraction", model="deepseek-v4-flash")
    # the no-explicit resolution (model layer over the shared floor), never the
    # sentinel and never a crash
    expected = explain_setting(
        "auto_compact_fraction", model="deepseek-v4-flash", explicit=None
    ).value
    assert value == expected


def test_resolve_setting_still_reads_explicit_value_in_full_profile() -> None:
    """In a full (profile-less) process — the agent's own — the explicit value
    still wins: the degradation must not leak into the owner process."""
    explicit = get_field("auto_compact_fraction")
    value = resolve_setting("auto_compact_fraction", model="deepseek-v4-flash")
    expected = explain_setting(
        "auto_compact_fraction", model="deepseek-v4-flash", explicit=explicit
    ).value
    assert value == expected
