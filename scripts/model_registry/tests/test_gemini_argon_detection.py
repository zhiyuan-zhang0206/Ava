from __future__ import annotations

from scripts.model_registry.check_model_updates import SOURCES, compare_models


def test_gemini_4_argon_is_classified_not_dropped_as_other() -> None:
    source = SOURCES["gemini"]
    registry = {"gemini-4-argon": type("Spec", (), {"provider": "gemini"})()}

    comparison = compare_models(
        source,
        ["gemini-4-argon", "gemini-4.1-argon", "gemini-3.9-argon", "gemini-pro-latest"],
        registry,
    )

    assert comparison.candidates == ["gemini-4.1-argon"]
    assert comparison.suppressed == ["gemini-3.9-argon"]
    assert comparison.series_models["gemini-4.1-argon"] == ["gemini-4-argon"]
    assert comparison.other_ids == ["gemini-pro-latest"]


def test_gemini_argon_preview_is_its_own_series() -> None:
    source = SOURCES["gemini"]
    registry = {"gemini-4-argon": type("Spec", (), {"provider": "gemini"})()}

    comparison = compare_models(source, ["gemini-4.1-argon-preview"], registry)

    assert comparison.candidates == ["gemini-4.1-argon-preview"]
    assert comparison.suppressed == []
    assert comparison.series_models["gemini-4.1-argon-preview"] == []
