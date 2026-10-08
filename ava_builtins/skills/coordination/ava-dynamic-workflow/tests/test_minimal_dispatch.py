"""Behavioral contracts for the small dynamic-workflow reference."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REFERENCE = Path(__file__).resolve().parents[1] / "references/minimal_dispatch.py"
TASKS = [{"id": "one", "prompt": "Analyze input one"}, {"id": "two", "prompt": "Analyze input two"}]


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    spec = importlib.util.spec_from_file_location("minimal_dispatch_example", REFERENCE)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[str] = []

    def spawn(*, prompt: str) -> int:
        calls.append(prompt)
        return 100 + len(calls)

    monkeypatch.setattr(module, "ava", SimpleNamespace(agents=SimpleNamespace(spawn=spawn)))
    return module, calls


def write_result(root: Path, task: dict[str, str], **overrides: Any) -> None:
    value = {
        "task_id": task["id"],
        "input_hash": hashlib.sha256(task["prompt"].encode()).hexdigest(),
        "result": {"evidence": "verified"},
    }
    value.update(overrides)
    (root / f"{task['id']}.json").write_text(json.dumps(value))


def test_reentry_preserves_known_peers_and_completed_results(
    dispatch: tuple[Any, list[str]], tmp_path: Path
) -> None:
    module, calls = dispatch
    first = module.run(tmp_path, TASKS)
    assert first["pending"] == {"one": 101, "two": 102}
    assert module.run(tmp_path, TASKS) == first
    assert len(calls) == 2
    for task in TASKS:
        write_result(tmp_path, task)
    before = (tmp_path / "one.json").read_bytes()
    done = module.run(tmp_path, TASKS)
    assert set(done["results"]) == {"one", "two"}
    assert done["pending"] == {}
    assert len(calls) == 2
    assert (tmp_path / "one.json").read_bytes() == before


def test_spawn_failure_persists_ambiguity_and_stops_batch(
    dispatch: tuple[Any, list[str]], tmp_path: Path
) -> None:
    module, calls = dispatch
    spawn = module.ava.agents.spawn

    def fail_second(*, prompt: str) -> int:
        if calls:
            raise ConnectionError("receipt unavailable")
        return spawn(prompt=prompt)

    module.ava.agents.spawn = fail_second
    with pytest.raises(ConnectionError):
        module.run(tmp_path, TASKS)
    state = json.loads((tmp_path / "dispatch.json").read_text())
    assert state["one"]["agent_id"] == 101
    assert state["two"]["phase"] == "dispatching"
    with pytest.raises(RuntimeError, match="ambiguous dispatch"):
        module.run(tmp_path, TASKS)
    assert len(calls) == 1


def test_crash_after_acceptance_does_not_spawn_again(
    dispatch: tuple[Any, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, calls = dispatch
    save = module._save

    def crash_receipt(path: Path, state: dict[str, Any]) -> None:
        if state.get("one", {}).get("phase") == "assigned":
            raise OSError("disk write failed after spawn")
        save(path, state)

    monkeypatch.setattr(module, "_save", crash_receipt)
    with pytest.raises(OSError):
        module.run(tmp_path, TASKS)
    with pytest.raises(RuntimeError, match="ambiguous dispatch"):
        module.run(tmp_path, TASKS)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "overrides", [{"input_hash": "old-input"}, {"task_id": "other"}, {"result": "partial"}]
)
def test_invalid_result_stops_before_any_spawn(
    dispatch: tuple[Any, list[str]], tmp_path: Path, overrides: dict[str, Any]
) -> None:
    module, calls = dispatch
    write_result(tmp_path, TASKS[1], **overrides)
    with pytest.raises(ValueError, match="invalid or stale"):
        module.run(tmp_path, TASKS)
    assert calls == []


def test_changed_task_cannot_adopt_old_receipt(
    dispatch: tuple[Any, list[str]], tmp_path: Path
) -> None:
    module, calls = dispatch
    module.run(tmp_path, TASKS)
    changed = [{"id": "one", "prompt": "Different source input"}, TASKS[1]]
    with pytest.raises(ValueError, match="changed"):
        module.run(tmp_path, changed)
    assert len(calls) == 2


def test_unknown_phase_fails_before_dispatch(
    dispatch: tuple[Any, list[str]], tmp_path: Path
) -> None:
    module, calls = dispatch
    state = {
        "one": {
            "input_hash": hashlib.sha256(TASKS[0]["prompt"].encode()).hexdigest(),
            "phase": "typo",
        }
    }
    (tmp_path / "dispatch.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="unknown dispatch phase"):
        module.run(tmp_path, TASKS)
    assert calls == []
