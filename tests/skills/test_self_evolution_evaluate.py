"""The self-evolution evaluator requests the shared eval-isolation boundary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

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
