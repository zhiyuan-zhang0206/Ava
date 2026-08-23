"""The self-evolution evaluator requests the shared eval-isolation boundary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import ava


def _evaluate_module() -> ModuleType:
    path = Path(".agents/skills/ava-self-evolution/reference/evaluate.py")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("test_evaluate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("model", "expected_overlay"),
    [
        (None, {"eval_isolation": True}),
        ("claude-sonnet-5", {"eval_isolation": True, "llm_model": "claude-sonnet-5"}),
    ],
)
def test_launch_spawns_eval_isolated_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str | None,
    expected_overlay: dict[str, object],
) -> None:
    evaluate = _evaluate_module()
    overlays: list[dict[str, object] | None] = []

    def _spawn(**kwargs: object) -> int:
        config_overlay = kwargs["config_overlay"]
        assert isinstance(config_overlay, dict)
        overlays.append(cast(dict[str, object], config_overlay))
        return 42

    monkeypatch.setattr(
        ava.agents,
        "spawn",
        _spawn,
    )
    monkeypatch.setattr(evaluate, "_eval_dir", lambda: tmp_path)

    state = evaluate.launch(
        "ava-goal",
        [{"agent_id": 9, "task_prompt": "Inspect the output", "tools_called": []}],
        model=model,
    )

    assert overlays == [expected_overlay]
    assert state["runs"][0]["eval_agent_id"] == 42
    assert state["runs"][0]["original_tools_called"] == []


@pytest.mark.parametrize(
    ("original", "replayed", "expected"),
    [
        (
            {"tools_called": {"ava.files.read": 1}},
            {"turns": 1, "final_output": "done", "tools_called": {}},
            False,
        ),
        (
            {"tools_called": {"ava.files.read": 1}},
            {
                "turns": 1,
                "final_output": "done",
                "tools_called": {"ava.files.read": 1, "ava.web.search": 1},
            },
            True,
        ),
        (
            {"tools_called": {"ava.files.read": 1}},
            {"turns": 1, "final_output": "done", "tools_called": {"ava.shell.run": 1}},
            False,
        ),
        (
            {"tools_called": {}},
            {"turns": 1, "final_output": "done", "tools_called": {}},
            True,
        ),
        (
            {"tools_called": {}},
            {"turns": 0, "final_output": "done", "tools_called": {}},
            False,
        ),
        (
            {"tools_called": {}},
            {"turns": 1, "final_output": "   ", "tools_called": {}},
            False,
        ),
    ],
)
def test_verify_replay(
    original: dict[str, object], replayed: dict[str, object], expected: bool
) -> None:
    evaluate = _evaluate_module()

    ok, _reason = evaluate.verify_replay(original, replayed)

    assert ok is expected


def test_gather_excludes_invalid_replays_from_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluate = _evaluate_module()
    state: dict[str, Any] = {
        "skill": "ava-goal",
        "stamp": "2026-08-23T00-00-00Z",
        "runs": [
            {"eval_agent_id": 41, "source_agent_id": 1, "original_tools_called": {}},
            {
                "eval_agent_id": 42,
                "source_agent_id": 2,
                "original_tools_called": {"ava.files.read": 1},
            },
        ],
        "skipped": [],
    }
    records: dict[int, dict[str, Any]] = {
        41: {
            "label": "ok",
            "turns": 1,
            "final_output": "done",
            "tools_called": {},
            "leak_audit": [],
            "invalidated": False,
        },
        42: {
            "label": "ok",
            "turns": 1,
            "final_output": "done",
            "tools_called": {},
            "leak_audit": [],
            "invalidated": False,
        },
    }

    def _poll(_state: dict[str, Any]) -> dict[str, list[int]]:
        return {"done": [41, 42], "pending": []}

    def _collect_one(agent_id: int, **_kwargs: object) -> dict[str, Any]:
        return records[agent_id]

    def _scores(rec: dict[str, Any]) -> dict[str, float]:
        if rec is records[41]:
            return {"completion": 1.0, "efficiency": 1.0, "overall": 1.0}
        return {"completion": 0.0, "efficiency": 0.0, "overall": 0.0}

    def _leak_paths(_agent_id: int) -> Any:
        return object()

    monkeypatch.setattr(evaluate, "poll", _poll)
    monkeypatch.setattr(evaluate, "collect_one", _collect_one)
    monkeypatch.setattr(evaluate, "_leak_paths", _leak_paths)
    monkeypatch.setattr(
        evaluate,
        "scores",
        _scores,
    )

    report = evaluate.gather(state)

    assert report["n"] == 1
    assert report["mean"] == {"completion": 1.0, "efficiency": 1.0, "overall": 1.0}
    assert [entry["valid"] for entry in report["per_task"]] == [True, False]
    assert report["invalid"][0]["eval_agent_id"] == 42


def test_gather_excludes_leaked_replays_from_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluate = _evaluate_module()
    state: dict[str, Any] = {
        "skill": "ava-goal",
        "stamp": "test",
        "runs": [
            {"eval_agent_id": 42, "source_agent_id": 1, "original_tools_called": {}},
            {"eval_agent_id": 43, "source_agent_id": 2, "original_tools_called": {}},
        ],
        "skipped": [],
    }
    records: dict[int, dict[str, Any]] = {
        42: {
            "agent_id": 42,
            "label": "ok",
            "turns": 1,
            "final_output": "done",
            "tools_called": {},
            "leak_audit": [],
            "invalidated": False,
        },
        43: {
            "agent_id": 43,
            "label": "ok",
            "turns": 1,
            "final_output": "done",
            "tools_called": {},
            "leak_audit": [{"surface": "results", "evidence": "read", "tool": None}],
            "invalidated": True,
        },
    }

    def _poll(_state: dict[str, Any]) -> dict[str, list[int]]:
        return {"done": [42, 43], "pending": []}

    def _collect_one(agent_id: int, **_kwargs: object) -> dict[str, Any]:
        return records[agent_id]

    def _scores(rec: dict[str, Any]) -> dict[str, float]:
        score = 1.0 if rec["agent_id"] == 42 else 0.0
        return {"completion": score, "efficiency": score, "overall": score}

    def _leak_paths(_agent_id: int) -> Any:
        return object()

    monkeypatch.setattr(evaluate, "poll", _poll)
    monkeypatch.setattr(evaluate, "collect_one", _collect_one)
    monkeypatch.setattr(evaluate, "_leak_paths", _leak_paths)
    monkeypatch.setattr(evaluate, "scores", _scores)

    report = evaluate.gather(state)

    assert report["n"] == 1
    assert report["mean"]["overall"] == 1.0
    assert report["invalidated"] == 1
    assert report["per_task"][1]["invalidated"] is True
    assert report["per_task"][1]["leak_audit"] == records[43]["leak_audit"]
    assert report["invalid"][0]["verification"]["reason"] == "leak"
