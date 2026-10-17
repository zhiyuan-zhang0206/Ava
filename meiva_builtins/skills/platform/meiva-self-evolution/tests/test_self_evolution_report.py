from __future__ import annotations

import sys
from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script


def _load(script: str) -> Any:
    before = sys.path[:]
    try:
        return cast(Any, load_skill_script("platform", "meiva-self-evolution", "scripts", script))
    finally:
        sys.path[:] = before


@pytest.fixture()
def aggregate_mod() -> Any:
    return _load("aggregate.py")


@pytest.fixture()
def report_mod() -> Any:
    return _load("render_report.py")


def test_abstained_mean_yields_no_score_dimensions(aggregate_mod: Any) -> None:
    before = {"completion": 0.70, "efficiency": 0.60, "overall": 0.67}

    assert (
        aggregate_mod.compute_score_dimensions(
            before, {"completion": None, "efficiency": None, "overall": None}
        )
        == []
    )
    assert aggregate_mod.compute_score_dimensions(before, {}) == []


def test_scored_mean_keeps_the_dimension_list(aggregate_mod: Any) -> None:
    dims = aggregate_mod.compute_score_dimensions(
        {"completion": 0.70, "efficiency": 0.60, "overall": 0.67},
        {"completion": 0.80, "efficiency": 0.65, "overall": 0.75},
    )

    assert [d["label"] for d in dims] == ["completion", "efficiency", "overall"]
    assert dims[2]["delta"] == 0.08
    assert dims[2]["delta_str"] == "+0.080"


def test_report_shows_na_when_no_skill_was_scored(report_mod: Any) -> None:
    data = report_mod.build_report_data(
        week="2026-W42",
        changes=[],
        score_skills=[{"skill": "meiva-workflow", "dimensions": []}],
        failure_clusters=[],
    )

    assert data["overall_delta"] == "n/a"


def test_report_keeps_the_signed_delta_when_scored(report_mod: Any) -> None:
    data = report_mod.build_report_data(
        week="2026-W42",
        changes=[],
        score_skills=[
            {
                "skill": "meiva-workflow",
                "dimensions": [{"label": "overall", "delta": 0.05}],
            }
        ],
        failure_clusters=[],
    )

    assert data["overall_delta"] == "+0.050"
