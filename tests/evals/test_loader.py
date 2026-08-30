"""Loader contract tests for the schema-v1 JSONL evalset loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.loader import EvalCaseV1, load_evalset

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_EVALSET = REPO_ROOT / "evals" / "cases" / "adversarial" / "adversarial-weekly-v1.jsonl"


def _valid_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "ava-adversarial-c001",
        "input": {"scenario_constructor": "schedules.adversarial_eval_cases:write_scenario"},
        "expected": {"facts": {"check_target": "key-verify.txt"}},
        "grader": {
            "type": "artifact-audit",
            "impl": "schedules.adversarial_eval_cases:audit_case",
            "grader_version": "1",
        },
        "meta": {
            "schema_version": "1",
            "line": "ava",
            "family": "document-authority",
            "created_at": "2026-08-31T07:00:00+08:00",
        },
    }
    row.update(overrides)
    return row


def _write(tmp_path: Path, rows: list[Any], *, raw: str | None = None) -> Path:
    path = tmp_path / "cases.jsonl"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_loads_the_reference_evalset() -> None:
    """The migrated c001-c009 dataset must parse clean and keep its families."""
    cases = load_evalset(REFERENCE_EVALSET)
    assert len(cases) == 9
    assert all(isinstance(c, EvalCaseV1) for c in cases)
    assert [c.id for c in cases] == [f"ava-adversarial-c00{i}" for i in range(1, 10)]
    assert cases[0].meta["family"] == "document-authority"
    assert cases[-1].meta["family"] == "evidence-backed-claim"
    assert all(c.meta["migrated_from"] == c.id.rsplit("-", 1)[-1] for c in cases)


def test_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    path = _write(tmp_path, [], raw="# header\n\n" + json.dumps(_valid_row()) + "\n")
    assert len(load_evalset(path)) == 1


def test_rejects_missing_required_field(tmp_path: Path) -> None:
    row = _valid_row()
    row.pop("expected")
    path = _write(tmp_path, [row])
    with pytest.raises(ValueError, match=r"missing required field 'expected'"):
        load_evalset(path)


def test_rejects_duplicate_id(tmp_path: Path) -> None:
    path = _write(tmp_path, [_valid_row(), _valid_row()])
    with pytest.raises(ValueError, match="duplicate id"):
        load_evalset(path)


@pytest.mark.parametrize(
    "bad_id",
    [
        "ava-adversarial-c1",
        "ava-adversarial-c01",
        "ava-c001",
        "ava-adversarial-c0001",
        "ava-adversarial-C001",
    ],
)
def test_rejects_nonconforming_id(tmp_path: Path, bad_id: str) -> None:
    row = _valid_row(id=bad_id)
    row["meta"]["line"] = bad_id.split("-", 1)[0]
    path = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="id"):
        load_evalset(path)


def test_rejects_line_meta_mismatch(tmp_path: Path) -> None:
    meta = _valid_row()["meta"]
    meta["line"] = "monsora"
    path = _write(tmp_path, [_valid_row(meta=meta)])
    with pytest.raises(ValueError, match=r"meta.line must equal"):
        load_evalset(path)


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    meta = _valid_row()["meta"]
    meta["schema_version"] = "2"
    path = _write(tmp_path, [_valid_row(meta=meta)])
    with pytest.raises(ValueError, match=r"schema_version"):
        load_evalset(path)


def test_rejects_missing_facts(tmp_path: Path) -> None:
    path = _write(tmp_path, [_valid_row(expected={"rubric": "no facts here"})])
    with pytest.raises(ValueError, match=r"expected.facts"):
        load_evalset(path)


def test_rejects_unknown_grader_type(tmp_path: Path) -> None:
    grader = _valid_row()["grader"]
    grader["type"] = "magic"
    path = _write(tmp_path, [_valid_row(grader=grader)])
    with pytest.raises(ValueError, match=r"unknown grader type"):
        load_evalset(path)


def test_rejects_non_object_row(tmp_path: Path) -> None:
    path = _write(tmp_path, [], raw="[1, 2, 3]\n")
    with pytest.raises(ValueError, match=r"row must be a JSON object"):
        load_evalset(path)


def test_rejects_invalid_json_line(tmp_path: Path) -> None:
    path = _write(tmp_path, [], raw="{not json\n")
    with pytest.raises(ValueError, match=r"invalid JSON"):
        load_evalset(path)
