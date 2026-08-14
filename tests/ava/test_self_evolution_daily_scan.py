"""Unit tests for the self-evolution daily scan (reference/daily_scan.py).

The reference scripts are standalone (the skill dir has a hyphen, so they are
not importable as a package); the test adds the reference dir to sys.path and
imports the module directly. `collect` is stubbed — no DB, no filesystem.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REF_DIR = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-evolution"
    / "reference"
)


@pytest.fixture()
def daily_scan() -> Any:
    """The module under test, imported with its reference dir on sys.path.

    importlib (not a static import) so pyright does not try to resolve the
    reference dir at analysis time; the cast keeps the module's member types
    unknown-but-Any instead of erroring on them.
    """
    sys.path.insert(0, str(REF_DIR))
    try:
        return cast(Any, importlib.import_module("daily_scan"))
    finally:
        sys.path.remove(str(REF_DIR))


def _record(label: str, agent_id: int = 1, **overrides: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "agent_id": agent_id,
        "week": "2026-08-13",
        "spawner": "user",
        "task_prompt": "",
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "transcript": [],
        "final_output": "done",
        "turns": 3,
        "exec_failed": 0,
        "last_exec_failed": False,
        "compactions": 0,
        "breached": False,
        "terminated": False,
        "label": label,
    }
    rec.update(overrides)
    return rec


def test_alert_exit_is_2_when_any_run_is_bad(daily_scan: Any) -> None:
    ds = daily_scan
    assert ds.alert_exit([_record("ok"), _record("ok")]) == 0
    assert ds.alert_exit([_record("ok"), _record("fumbled")]) == 2
    assert ds.alert_exit([_record("failed")]) == 2


def test_alert_exit_is_2_when_no_runs_collected(daily_scan: Any) -> None:
    """An empty dataset means the data source broke — never "nothing to act
    on" (2026-08-14: PG events froze and the scan green-lit an empty day)."""
    ds = daily_scan
    assert ds.alert_exit([]) == 2
    rendered = ds.render([], Path("daily-2026-08-14.jsonl"), 1)
    assert "0 runs collected" in rendered


def test_render_lists_bad_runs_with_their_signals(daily_scan: Any) -> None:
    ds = daily_scan
    records = [
        _record("ok", agent_id=1),
        _record(
            "failed",
            agent_id=2,
            task_prompt="Fix the suite",
            corrections=["不对，重做"],
            exec_failed=4,
            last_exec_failed=True,
        ),
    ]
    out = ds.render(records, Path("daily.jsonl"), 1)
    assert "runs: 2 (ok 1 / fumbled 0 / failed 1)" in out
    assert "ALERT — 1 run(s) worth mining:" in out
    assert "#2 failed" in out
    assert "1 user correction(s)" in out
    assert "4 failed exec(s)" in out
    assert "last exec failed" in out
    assert "Fix the suite" in out


def test_render_is_quiet_on_clean_day(daily_scan: Any) -> None:
    ds = daily_scan
    out = ds.render([_record("ok", agent_id=1), _record("ok", agent_id=2)], Path("d.jsonl"), 1)
    assert "ALERT" not in out
