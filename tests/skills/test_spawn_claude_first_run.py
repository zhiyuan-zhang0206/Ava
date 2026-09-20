"""First-run preset contracts for the Claude Code launcher (task #3996)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REFERENCE = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-use-claude-code-and-codex"
    / "reference"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_first_run_under_test", _REFERENCE / "spawn_claude.py")


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _claude_json(home: Path) -> Path:
    return home / ".claude.json"


def test_fresh_home_gets_both_files_preset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    spawn_claude._preset_claude_first_run(home)
    out = capsys.readouterr().out
    assert "+ preset: ~/.claude/settings.json" in out
    assert "+ preset: ~/.claude.json" in out
    assert json.loads(_settings(home).read_text()) == {"skipDangerousModePermissionPrompt": True}
    assert json.loads(_claude_json(home).read_text()) == {"fullscreenUpsellSeenCount": 3}
    assert not list(home.rglob("*.bak-*"))


def test_existing_config_is_preserved_and_backed_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text(json.dumps({"env": {"A": "1"}, "model": "opus"}))
    _claude_json(home).write_text(json.dumps({"fullscreenUpsellSeenCount": 1, "userID": "x"}))
    spawn_claude._preset_claude_first_run(home)
    settings = json.loads(_settings(home).read_text())
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["env"] == {"A": "1"} and settings["model"] == "opus"
    claude_json = json.loads(_claude_json(home).read_text())
    assert claude_json["fullscreenUpsellSeenCount"] == 3 and claude_json["userID"] == "x"
    backups = sorted(p.name for p in home.rglob("*.bak-*"))
    assert len(backups) == 2


def test_second_call_is_a_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    spawn_claude._preset_claude_first_run(home)
    capsys.readouterr()
    before = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    spawn_claude._preset_claude_first_run(home)
    out = capsys.readouterr().out
    assert out.count("(already preset") == 2
    after = {p: p.stat().st_mtime_ns for p in home.rglob("*") if p.is_file()}
    assert before == after
    assert not list(home.rglob("*.bak-*"))


def test_count_at_or_above_threshold_is_satisfied(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _claude_json(home).write_text(json.dumps({"fullscreenUpsellSeenCount": 5}))
    spawn_claude._preset_claude_first_run(home)
    assert json.loads(_claude_json(home).read_text())["fullscreenUpsellSeenCount"] == 5


@pytest.mark.parametrize("break_settings", [True, False])
def test_unparsable_file_is_backed_up_and_raises(tmp_path: Path, break_settings: bool) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if break_settings:
        _settings(home).parent.mkdir(parents=True)
        _settings(home).write_text("{not json")
        target = _settings(home)
    else:
        _claude_json(home).write_text("[1,2")
        target = _claude_json(home)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        spawn_claude._preset_claude_first_run(home)
    assert target.read_text() == ("{not json" if break_settings else "[1,2")
    assert list(home.rglob("*.bak-*"))
