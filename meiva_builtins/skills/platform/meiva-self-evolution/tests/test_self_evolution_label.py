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
def label_mod() -> Any:
    return _load("label.py")


@pytest.fixture()
def mine_mod() -> Any:
    return _load("mine.py")


def _record(**overrides: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "tools_called": {},
        "terminated": True,
        "final_output": "delivered",
        "turns": 4,
        "last_exec_failed": False,
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "exec_failed": 0,
        "compactions": 0,
        "evidence_grade": "complete",
    }
    rec.update(overrides)
    return rec


def test_complete_evidence_without_negatives_is_ok(label_mod: Any) -> None:
    assert label_mod.label(_record()) == "ok"


def test_complete_evidence_keeps_the_fumble_threshold(label_mod: Any) -> None:
    assert label_mod.label(_record(followup_prompts=["again"])) == "ok"
    assert label_mod.label(_record(followup_prompts=["again", "again"])) == "fumbled"


def test_missing_evidence_is_unknown_even_with_exec_failures(label_mod: Any) -> None:
    rec = _record(evidence_grade="none", exec_failed=9, last_exec_failed=True)

    assert label_mod.label(rec) == "unknown"


def test_partial_evidence_is_unknown(label_mod: Any) -> None:
    assert label_mod.label(_record(evidence_grade="partial")) == "unknown"


def test_record_without_a_grade_keeps_the_rule_label(label_mod: Any) -> None:
    broken = _record(terminated=True, final_output="", turns=3)
    broken.pop("evidence_grade")
    clean = _record()
    clean.pop("evidence_grade")

    assert label_mod.label(broken) == "failed"
    assert label_mod.label(clean) == "ok"


def test_mine_skips_unknown_runs(mine_mod: Any) -> None:
    records = [
        {"label": "unknown", "skills_touched": ["skill-unknown"]},
        {"label": "failed", "skills_touched": ["skill-failed"]},
        {"label": "fumbled", "skills_touched": ["skill-fumbled"]},
        {"label": "ok", "skills_touched": ["skill-ok"]},
    ]

    assert set(mine_mod.cluster_by_skill(records)) == {"skill-failed", "skill-fumbled"}
