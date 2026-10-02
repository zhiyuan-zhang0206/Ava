"""The installed spawn scripts support both runtime launch signatures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SKILL_DIR = Path(__file__).parents[3] / "ava_builtins" / "skills" / "ava-use-other-agents"


def _script(tool: str) -> ModuleType:
    path = _SKILL_DIR / "scripts" / f"spawn_{tool}.py"
    spec = importlib.util.spec_from_file_location(f"spawn_{tool}_compat_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("runtime", ["old", "new"])
def test_spawn_dispatches_to_runtime_layout(
    tool: str, runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(tool)
    calls: list[Path] = []
    monkeypatch.setenv("HOME", str(tmp_path))

    if runtime == "old":

        def old_launch(*args: object, reference_dir: Path, **kwargs: object) -> int:
            calls.append(reference_dir)
            return 0

        launch = old_launch
    else:

        def new_launch(*args: object, skill_dir: Path, **kwargs: object) -> int:
            calls.append(skill_dir)
            return 0

        launch = new_launch
    monkeypatch.setattr(getattr(script, tool), "launch", launch)
    monkeypatch.setattr(sys, "argv", [f"spawn_{tool}.py", str(tmp_path)])
    assert script.main() == 0

    if runtime == "old":
        reference = calls[0]
        assert reference.name == "reference"
        assert reference.is_relative_to(tmp_path / ".cache" / "ava" / "skill-spawn-compat")
        assert (reference / "collaboration_protocol.md").resolve() == (
            _SKILL_DIR / "references" / "collaboration_protocol.md"
        )
        if tool == "codex":
            assert (
                reference / "watch_work.py"
            ).resolve() == _SKILL_DIR / "scripts" / "watch_work.py"
        else:
            assert (reference / "ava-relay").resolve() == _SKILL_DIR / "scripts" / "ava-relay"
        assert script.main() == 0
        assert calls == [reference, reference]
    else:
        assert calls == [_SKILL_DIR]
