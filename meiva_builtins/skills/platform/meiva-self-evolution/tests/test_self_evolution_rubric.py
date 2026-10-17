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
def rubric_mod() -> Any:
    return _load("rubric.py")


@pytest.fixture()
def evaluate_mod() -> Any:
    return _load("evaluate.py")


def _record(**overrides: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "final_output": "delivered",
        "exec_ok": 8,
        "exec_failed": 2,
        "turns": 10,
        "tokens_in": 30_000,
        "tokens_out": 10_000,
        "last_exec_failed": False,
        "followup_prompts": [],
        "compactions": 0,
        "evidence_grade": "complete",
    }
    rec.update(overrides)
    return rec


def test_zero_telemetry_record_abstains(rubric_mod: Any) -> None:
    rec = _record(
        evidence_grade="none",
        final_output="one polished sentence",
        exec_ok=0,
        exec_failed=0,
        turns=0,
        tokens_in=0,
        tokens_out=0,
    )

    assert rubric_mod.scores(rec) == {"completion": None, "efficiency": None, "overall": None}


def test_partial_grade_abstains(rubric_mod: Any) -> None:
    rec = _record(evidence_grade="partial")

    assert rubric_mod.scores(rec) == {"completion": None, "efficiency": None, "overall": None}


def test_missing_terms_renormalize_completion(rubric_mod: Any) -> None:
    rec = _record(final_output="", exec_ok=0, exec_failed=0, turns=4, tokens_in=0, tokens_out=0)

    scores = rubric_mod.scores(rec)

    assert scores["completion"] == round(0.15 / (0.55 + 0.15), 3)
    assert scores["efficiency"] == round((0.20 + 0.20 + 0.15 * (8 / 12)) / 0.55, 3)
    assert scores["overall"] is not None


def test_complete_record_with_exec_keeps_previous_scores(rubric_mod: Any) -> None:
    old_completion = round(0.55 * 1.0 + 0.30 * (8 / 10) + 0.15 * 1.0, 3)
    old_efficiency = round(
        0.30 * (8 / 10) + 0.20 * 1.0 + 0.20 * 1.0 + 0.15 * (8 / 18) + 0.15 * (60_000 / 100_000),
        3,
    )
    old_overall = round(0.7 * old_completion + 0.3 * old_efficiency, 3)

    assert rubric_mod.scores(_record()) == {
        "completion": old_completion,
        "efficiency": old_efficiency,
        "overall": old_overall,
    }


def test_overall_is_none_when_efficiency_abstains(rubric_mod: Any) -> None:
    rec = _record(exec_ok=0, exec_failed=0, turns=0, tokens_in=0, tokens_out=0)

    scores = rubric_mod.scores(rec)

    assert scores["completion"] is not None
    assert scores["efficiency"] is None
    assert scores["overall"] is None


def test_legacy_record_without_a_grade_keeps_scoring(rubric_mod: Any) -> None:
    rec = _record()
    rec.pop("evidence_grade")

    assert rubric_mod.scores(rec)["overall"] == rubric_mod.scores(_record())["overall"]


def test_score_summary_counts_scored_and_abstained(evaluate_mod: Any) -> None:
    scored = [{"completion": 0.8, "efficiency": 0.6, "overall": 0.74}]
    scored.append({"completion": 1.0, "efficiency": 0.5, "overall": 0.85})
    abstained = {"completion": None, "efficiency": None, "overall": None}

    summary = evaluate_mod._score_summary([*scored, abstained])

    assert summary["n_scored"] == 2
    assert summary["n_abstained"] == 1
    assert summary["mean"] == {"completion": 0.9, "efficiency": 0.55, "overall": 0.795}


def test_all_abstained_batch_has_no_mean(evaluate_mod: Any) -> None:
    abstained = {"completion": None, "efficiency": None, "overall": None}

    summary = evaluate_mod._score_summary([abstained, abstained])

    assert summary["n_scored"] == 0
    assert summary["n_abstained"] == 2
    assert summary["mean"] == {"completion": None, "efficiency": None, "overall": None}
