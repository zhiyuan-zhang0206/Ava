from __future__ import annotations

from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

rubric = cast(Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "rubric.py"))


def record(**overrides: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "task_prompt": "Summarise the incident.",
        "followup_prompts": [],
        "final_output": "Done.",
        "turns": 6,
        "tokens_in": 12_000,
        "tokens_out": 2_000,
        "exec_ok": 4,
        "exec_failed": 0,
        "last_exec_failed": False,
        "compactions": 0,
        "tools_called": {"meiva.shell.run": 3},
        "terminated": True,
    }
    rec.update(overrides)
    return rec


def test_scorable_trace_returns_numbers() -> None:
    scored = rubric.scores(record())
    assert scored["abstain"] is None if "abstain" in scored else True
    for key in ("completion", "efficiency", "overall"):
        assert isinstance(scored[key], float)


def test_empty_trace_no_longer_scores_perfect() -> None:
    empty = record(
        task_prompt="",
        final_output="",
        turns=0,
        tokens_in=0,
        tokens_out=0,
        exec_ok=0,
        exec_failed=0,
        compactions=0,
        tools_called={},
    )
    assert rubric.scores(empty)["overall"] is None
    assert rubric.abstain_reason(empty) == rubric.ABSTAIN_NO_TASK


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"task_prompt": "", "followup_prompts": []}, "no_task"),
        (
            {"turns": 0, "exec_ok": 0, "exec_failed": 0, "compactions": 0, "tools_called": {}},
            "no_telemetry",
        ),
        ({"terminated": False}, "no_terminal"),
    ],
)
def test_missing_precondition_abstains(overrides: dict[str, Any], reason: str) -> None:
    rec = record(**overrides)
    assert rubric.abstain_reason(rec) == reason
    scored = rubric.scores(rec)
    assert scored["abstain"] == reason
    assert scored["overall"] is None


def test_evidence_grade_is_the_owner_for_new_records() -> None:
    complete = record(evidence_grade="complete", unknown_reason=None)
    assert rubric.abstain_reason(complete) is None
    partial = record(evidence_grade="partial", unknown_reason="no_task")
    assert rubric.abstain_reason(partial) == "no_task"
    graded = record(evidence_grade="none", unknown_reason="no_telemetry")
    assert rubric.scores(graded)["abstain"] == "no_telemetry"


def test_followups_count_as_a_requirement() -> None:
    rec = record(task_prompt="", followup_prompts=["continue the sweep"])
    assert rubric.abstain_reason(rec) is None
