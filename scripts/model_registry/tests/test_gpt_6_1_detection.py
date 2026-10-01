"""Regression for GPT-6 minor releases in the daily model tracker."""

from __future__ import annotations

from scripts.model_registry.check_model_updates import SOURCES, compare_models


def test_gpt_6_1_sol_is_a_newer_same_series_candidate() -> None:
    source = SOURCES["gpt"]
    registry = {"gpt-6-sol": type("Spec", (), {"provider": "gpt"})()}

    comparison = compare_models(source, ["gpt-6.1-sol"], registry)

    assert comparison.candidates == ["gpt-6.1-sol"]
    assert comparison.suppressed == []
    assert comparison.other_ids == []
    assert comparison.series_models["gpt-6.1-sol"] == ["gpt-6-sol"]
