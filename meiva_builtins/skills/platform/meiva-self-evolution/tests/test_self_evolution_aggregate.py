from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script


@pytest.fixture()
def aggregate_mod() -> Any:
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    before = sys.path[:]
    sys.path.insert(0, str(scripts))
    try:
        yield cast(
            Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "aggregate.py")
        )
    finally:
        sys.path[:] = before


def _gather(
    path: Path, skill: str, mean: dict[str, float | None] | None, n: int | None = None
) -> None:
    state: dict[str, Any] = {"skill": skill, "stamp": path.stem, "mean": mean}
    if n is not None:
        state["n"] = n
    path.write_text(json.dumps(state), encoding="utf-8")


def _changes(tmp_path: Path) -> Path:
    changes = tmp_path / "changes.json"
    changes.write_text("[]", encoding="utf-8")
    return changes


def _run(aggregate_mod: Any, tmp_path: Path, eval_dir: Path) -> dict[str, Any]:
    out = tmp_path / "report.json"
    aggregate_mod.main(
        [
            "--changes",
            str(_changes(tmp_path)),
            "--week",
            "2026-W42",
            "--eval-dir",
            str(eval_dir),
            "--total-runs",
            "120",
            "--out",
            str(out),
        ]
    )
    return cast(dict[str, Any], json.loads(out.read_text(encoding="utf-8")))


def test_total_runs_is_required(aggregate_mod: Any, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        aggregate_mod.main(
            ["--changes", str(_changes(tmp_path)), "--week", "2026-W42", "--out", "r.json"]
        )

    assert excinfo.value.code == 2


def test_no_baseline_fabricates_no_delta(
    aggregate_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    _gather(
        eval_dir / "meiva-workflow-2026-10-01.json",
        "meiva-workflow",
        {"completion": 0.8, "efficiency": 0.6, "overall": 0.74},
        n=12,
    )

    report = _run(aggregate_mod, tmp_path, eval_dir)
    capsys.readouterr()

    assert report["overall_delta"] == "n/a"
    assert report["score_skills"] == [
        {"skill": "meiva-workflow", "dimensions": [], "n": 12, "unavailable": False}
    ]


def test_earlier_stamp_becomes_the_baseline(
    aggregate_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    _gather(
        eval_dir / "meiva-workflow-2026-10-01.json",
        "meiva-workflow",
        {"completion": 0.70, "efficiency": 0.60, "overall": 0.67},
        n=12,
    )
    _gather(
        eval_dir / "meiva-workflow-2026-10-08.json",
        "meiva-workflow",
        {"completion": 0.80, "efficiency": 0.65, "overall": 0.75},
        n=14,
    )

    report = _run(aggregate_mod, tmp_path, eval_dir)
    capsys.readouterr()

    assert report["overall_delta"] == "+0.080"
    assert [d["label"] for d in report["score_skills"][-1]["dimensions"]] == [
        "completion",
        "efficiency",
        "overall",
    ]


def test_zero_scored_batch_prints_no_delta(
    aggregate_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    _gather(
        eval_dir / "meiva-workflow-2026-10-01.json",
        "meiva-workflow",
        {"completion": 0.70, "efficiency": 0.60, "overall": 0.67},
        n=12,
    )
    _gather(
        eval_dir / "meiva-workflow-2026-10-08.json",
        "meiva-workflow",
        {"completion": 0.0, "efficiency": 0.0, "overall": 0.0},
        n=0,
    )

    report = _run(aggregate_mod, tmp_path, eval_dir)
    capsys.readouterr()

    assert report["overall_delta"] == "n/a"
    assert report["score_skills"][-1]["dimensions"] == []
    assert report["score_skills"][-1]["unavailable"] is True


def test_zero_scored_batch_never_becomes_the_baseline(
    aggregate_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    _gather(
        eval_dir / "meiva-workflow-2026-10-01.json",
        "meiva-workflow",
        {"completion": 0.0, "efficiency": 0.0, "overall": 0.0},
        n=0,
    )
    _gather(
        eval_dir / "meiva-workflow-2026-10-08.json",
        "meiva-workflow",
        {"completion": 0.80, "efficiency": 0.60, "overall": 0.74},
        n=4,
    )
    _gather(
        eval_dir / "meiva-workflow-2026-10-15.json",
        "meiva-workflow",
        {"completion": 0.90, "efficiency": 0.70, "overall": 0.82},
        n=5,
    )

    report = _run(aggregate_mod, tmp_path, eval_dir)
    capsys.readouterr()

    assert report["score_skills"][1]["dimensions"] == []
    assert report["overall_delta"] == "+0.080"


def test_missing_n_falls_back_to_a_none_mean(
    aggregate_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    _gather(
        eval_dir / "meiva-workflow-2026-10-01.json",
        "meiva-workflow",
        {"completion": 0.70, "efficiency": 0.60, "overall": 0.67},
    )
    _gather(
        eval_dir / "meiva-workflow-2026-10-08.json",
        "meiva-workflow",
        {"completion": None, "efficiency": None, "overall": None},
    )

    report = _run(aggregate_mod, tmp_path, eval_dir)
    capsys.readouterr()

    assert report["overall_delta"] == "n/a"
    assert report["score_skills"][-1]["dimensions"] == []
