"""Tests for .agents/skills/ava-deep-research/scripts/audit_research.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / ".agents"
    / "skills"
    / "ava-deep-research"
    / "scripts"
    / "audit_research.py"
)


def _run(state: dict, report: str | None = None) -> subprocess.CompletedProcess[str]:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps(state))
        cmd = [sys.executable, str(SCRIPT), "--state", str(state_path)]
        if report is not None:
            report_path = Path(td) / "report.md"
            report_path.write_text(report)
            cmd += ["--report", str(report_path)]
        return subprocess.run(  # noqa: S603 — argv is sys.executable + this repo's own script
            cmd, capture_output=True, text=True, check=False
        )


def _good_state() -> dict:
    return {
        "question": {
            "three_part": "studying X to find out Y, so the audience understands Z",
            "so_what": "if unanswered, the buyer loses money",
            "type": "practical",
            "evidence_bar": "decision-grade",
        },
        "plan": [
            {
                "id": "q1",
                "sub_question": "what is X",
                "queries": ["X overview"],
                "source_types": ["primary"],
                "status": "covered",
            }
        ],
        "sources": [
            {
                "id": 1,
                "url": "https://example.com/a",
                "title": "Source A",
                "publisher": "Example Corp",
                "date": "2026-01-01",
                "accessed_at": "2026-08-09T12:00:00Z",
                "kind": "primary",
            },
            {
                "id": 2,
                "url": "https://example.com/b",
                "title": "Source B",
                "publisher": "Example Corp",
                "date": "2026-02-01",
                "accessed_at": "2026-08-09T12:05:00Z",
                "kind": "secondary",
            },
        ],
        "learnings": [
            {
                "id": 1,
                "fact": "X is real.",
                "source_ids": [1, 2],
                "confidence": "consensus",
            }
        ],
        "claims": [{"claim": "X is real", "source_ids": [1, 2], "verification": "process"}],
    }


def test_clean_state_passes() -> None:
    res = _run(_good_state())
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK:" in res.stdout


def test_missing_section_fails() -> None:
    state = _good_state()
    del state["learnings"]
    res = _run(state)
    assert res.returncode == 1
    assert "missing required section" in res.stdout


def test_learning_without_source_fails() -> None:
    state = _good_state()
    state["learnings"][0]["source_ids"] = []
    res = _run(state)
    assert res.returncode == 1
    assert "source_ids must be a non-empty list" in res.stdout


def test_dangling_source_id_fails() -> None:
    state = _good_state()
    state["learnings"][0]["source_ids"] = [1, 99]
    res = _run(state)
    assert res.returncode == 1
    assert "source_id 99 not in sources" in res.stdout


def test_consensus_requires_two_sources() -> None:
    state = _good_state()
    state["learnings"][0]["source_ids"] = [1]
    res = _run(state)
    assert res.returncode == 1
    assert "consensus requires >= 2 distinct sources" in res.stdout


def test_source_without_accessed_at_fails() -> None:
    state = _good_state()
    del state["sources"][0]["accessed_at"]
    res = _run(state)
    assert res.returncode == 1
    assert "missing accessed_at" in res.stdout


def test_duplicate_url_fails() -> None:
    state = _good_state()
    state["sources"][1]["url"] = state["sources"][0]["url"]
    res = _run(state)
    assert res.returncode == 1
    assert "duplicate url" in res.stdout


def test_bad_confidence_fails() -> None:
    state = _good_state()
    state["learnings"][0]["confidence"] = "definitely"
    res = _run(state)
    assert res.returncode == 1
    assert "confidence" in res.stdout


def test_report_citation_resolves() -> None:
    res = _run(_good_state(), report="X is real [1, 2].\n\n## Sources\n")
    assert res.returncode == 0, res.stdout + res.stderr


def test_report_citation_to_unknown_source_fails() -> None:
    res = _run(_good_state(), report="X is real [7].\n")
    assert res.returncode == 1
    assert "citation [7] does not resolve" in res.stdout


def test_report_citation_with_spacing_resolves() -> None:
    res = _run(_good_state(), report="X is real [ 1 ,\n 2 ].\n")
    assert res.returncode == 0, res.stdout + res.stderr


def test_report_code_block_brackets_ignored() -> None:
    report = "X is real [1].\n\n```python\nidx = [99]\narr[3] = x\n```\n"
    res = _run(_good_state(), report=report)
    assert res.returncode == 0, res.stdout + res.stderr


def test_report_inline_code_brackets_ignored() -> None:
    res = _run(_good_state(), report="X is real [1]; use `arr[3]` there.\n")
    assert res.returncode == 0, res.stdout + res.stderr


def test_malformed_source_entry_fails_cleanly() -> None:
    state = _good_state()
    state["sources"][0] = "not a dict"
    res = _run(state)
    assert res.returncode in (1, 2)
    assert "Traceback" not in res.stderr


def test_budget_within_cap_passes() -> None:
    state = _good_state()
    state["meta"] = {"budget": {"max_fetches": 2}}
    res = _run(state)
    assert res.returncode == 0, res.stdout + res.stderr


def test_budget_exceeded_fails() -> None:
    state = _good_state()
    state["meta"] = {"budget": {"max_fetches": 1}}
    res = _run(state)
    assert res.returncode == 1
    assert "budget exceeded: 2 unique sources > meta.budget.max_fetches 1" in res.stdout


def test_budget_absent_is_not_checked() -> None:
    # backward compat: a state without meta.budget passes — no cap was pre-registered
    res = _run(_good_state())
    assert res.returncode == 0, res.stdout + res.stderr
    assert "budget exceeded" not in res.stdout


def test_budget_non_integer_is_ignored() -> None:
    state = _good_state()
    state["meta"] = {"budget": {"max_fetches": "2"}}  # malformed — no enforcement
    res = _run(state)
    assert res.returncode == 0, res.stdout + res.stderr
