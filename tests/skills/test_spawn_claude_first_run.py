"""First-run and launch safety contracts for the Claude Code launcher."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import threading
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


def test_claude_command_uses_home_fallback_when_session_path_has_no_claude(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".local" / "bin" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "launched:%s\\n" "$1"\n')
    executable.chmod(0o755)
    path = tmp_path / "empty-path"
    path.mkdir()

    command = spawn_claude._claude_command(tmp_path)
    result = subprocess.run(  # noqa: S603 - executes only the launcher command against a fake CLI
        ["/bin/bash", "-c", command],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "launched:--dangerously-skip-permissions\n"

    path_executable = path / "claude"
    path_executable.write_text('#!/bin/sh\nprintf "path:%s\\n" "$1"\n')
    path_executable.chmod(0o755)
    normal = subprocess.run(  # noqa: S603 - controlled PATH executable verifies precedence
        ["/bin/bash", "-c", command],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert normal.returncode == 0
    assert normal.stdout == "path:--dangerously-skip-permissions\n"


def test_claude_command_exits_when_executable_is_unavailable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "empty-path"
    path.mkdir()

    marker = tmp_path / "missing-claude"
    result = subprocess.run(  # noqa: S603 - isolated shell tests the fail-closed launcher command
        ["/bin/bash", "-c", spawn_claude._claude_command(tmp_path, failure_marker=marker)],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "claude executable not found in PATH or $HOME/.local/bin/claude" in result.stderr
    assert marker.read_text() == "claude executable not found\n"


def test_command_echo_is_not_a_missing_executable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command_echo = "shell $ " + spawn_claude._claude_command(tmp_path)
    ui = "Claude Code v2.1.278\n? for shortcuts\n"
    captures = iter((command_echo, ui, ui))

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return next(captures)

    def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)

    spawn_claude._wait_for_ready(7, timeout=1, failure_marker=tmp_path / "missing-claude")


def test_command_echo_without_ui_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command_echo = "shell $ " + spawn_claude._claude_command(tmp_path)

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return command_echo

    def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="Claude Code UI did not appear"):
        spawn_claude._wait_for_ready(7, timeout=0.01, failure_marker=tmp_path / "missing-claude")


def test_ready_accepts_recorded_claude_2_1_281_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = (
        " ▐▛███▛█   Claude Code v2.1.281\n"
        "▝▜██████▀  Opus 5.5 · Claude Pro\n"
        "  ▝▝ ▝▝    <cwd>\n"
        "  ... (usage note)\n"
        "─────────────────────────────────────────────\n"
        '\u276f\u00a0Try "fix lint errors"\n'
        "─────────────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return panel

    def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)

    spawn_claude._wait_for_ready(7, timeout=1)


def test_ready_accepts_other_unicode_spacing_without_a_fixed_suggestion() -> None:
    panel = 'Claude Code v2.1.281\n\u276f\u2009Try "explain this file"\nbypass permissions on\n'
    assert spawn_claude._claude_ui_ready(panel)


def test_ready_rejects_non_claude_panel_with_a_composer() -> None:
    panel = 'Other CLI v2.1.281\n\u276f\u00a0Try "fix lint errors"\nbypass permissions on\n'
    assert not spawn_claude._claude_ui_ready(panel)


def test_exited_session_reports_missing_executable_from_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "missing-claude"
    marker.write_text("claude executable not found\n")

    def _capture(_sid: int, *, scrollback: bool) -> str:
        raise ValueError("session ended")

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    with pytest.raises(
        RuntimeError, match=r"claude executable not found in PATH or \$HOME/\.local/bin/claude"
    ):
        spawn_claude._wait_for_ready(7, failure_marker=marker)
    assert (
        "claude executable not found in PATH or $HOME/.local/bin/claude" in capsys.readouterr().out
    )


def test_ready_requires_claude_ui_not_a_long_shell_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_screen = "exec claude: not found\n" + "bash prompt $ " * 10

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return shell_screen

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="Claude Code UI did not appear"):
        spawn_claude._wait_for_ready(7, timeout=0.01)


def test_ready_accepts_a_stable_claude_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    captures: list[int] = []

    def _capture(sid: int, *, scrollback: bool) -> str:
        assert sid == 7 and scrollback is False
        captures.append(sid)
        return "Claude Code v2.1.278\n? for shortcuts\n" + "Claude composer " * 10

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)
    spawn_claude._wait_for_ready(7, timeout=1)
    assert captures == [7, 7]


def test_ready_rejects_claude_setup_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return "Welcome to Claude Code\nChoose the text style that looks best with your terminal"

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)
    with pytest.raises(RuntimeError, match="Claude Code UI did not appear"):
        spawn_claude._wait_for_ready(7, timeout=0.01)


def test_ready_reports_when_the_session_exits_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        raise ValueError("session ended")

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    with pytest.raises(RuntimeError, match="exited before its UI appeared"):
        spawn_claude._wait_for_ready(7)


def test_supervised_launch_does_not_send_contract_to_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    killed: list[int] = []

    def _session_exists(_name: str) -> bool:
        return False

    def _pretrust(_workspace: Path) -> None:
        return None

    def _new(*, name: str, ttl: float) -> int:
        assert name and ttl == 3600
        return 7

    def _send(_sid: int, content: str) -> None:
        sent.append(content)

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return "error: claude executable not found in PATH or $HOME/.local/bin/claude\n"

    original_command = spawn_claude._claude_command

    def _missing_command(
        workspace: Path, caller_instance: str | None = None, *, failure_marker: Path
    ) -> str:
        failure_marker.write_text("claude executable not found\n")
        return original_command(workspace, caller_instance, failure_marker=failure_marker)

    def _kill(sid: int) -> None:
        killed.append(sid)

    monkeypatch.setattr(spawn_claude, "_session_exists", _session_exists)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(spawn_claude, "_claude_command", _missing_command)

    with pytest.raises(RuntimeError, match="claude executable not found"):
        spawn_claude._run_supervised_launch(
            tmp_path, tmp_path / "tasks.md", tmp_path / "work.md", 3600, None
        )

    assert len(sent) == 1
    assert killed == [7]


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


def _frozen_strftime(_format: str) -> str:
    return "20260920-204500"


def test_same_second_concurrent_presets_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 regression (Ava #3242 review): unique tmp/backup names, frozen clock, two threads."""
    monkeypatch.setattr(spawn_claude.time, "strftime", _frozen_strftime)
    failures: list[str] = []

    def worker(home: Path, barrier: threading.Barrier) -> None:
        try:
            barrier.wait(timeout=10)
            spawn_claude._preset_claude_first_run(home)
        except BaseException as exc:
            failures.append(f"{type(exc).__name__}: {exc}")

    for round_no in range(24):
        home = tmp_path / f"home{round_no}"
        (home / ".claude").mkdir(parents=True)
        settings = _settings(home)
        settings.write_text(json.dumps({"env": {"K": "v"}}))
        _claude_json(home).write_text(json.dumps({"userID": "u"}))
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=worker, args=(home, barrier)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not failures, failures
        merged = json.loads(settings.read_text())
        assert merged["env"] == {"K": "v"}
        assert merged["skipDangerousModePermissionPrompt"] is True
        claude_json = json.loads(_claude_json(home).read_text())
        assert claude_json["userID"] == "u"
        assert claude_json["fullscreenUpsellSeenCount"] == 3
        backups = [p.name for p in home.rglob("*.bak-*")]
        assert len(backups) == len(set(backups))
        assert not list(home.rglob("*.tmp"))


def test_existing_file_keeps_its_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text("{}")
    _settings(home).chmod(0o640)
    spawn_claude._preset_claude_first_run(home)
    assert stat.S_IMODE(_settings(home).stat().st_mode) == 0o640


_PANEL_2_1_283 = (
    " ▐▛███▜▌   Claude Code v2.1.283\n"
    "▝▜█████▛▘  Opus 5.5 (1M context)\n"
    "  ~/.ava-previews/run/home/workspaces/4\n"
    "────────────\n"
    "\u276f\n"
    "────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


def test_ready_accepts_bare_composer_of_recorded_claude_2_1_283_panel() -> None:
    # Captures strip trailing blanks, so the empty composer is a lone glyph.
    assert spawn_claude._claude_ui_ready(_PANEL_2_1_283)


def test_signed_out_claude_fails_fast_instead_of_timing_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A signed-out CLI still renders a ready panel; the launch command's
    # `claude auth status` preflight leaves the marker the wait refuses on.
    marker = tmp_path / "missing-claude"
    spawn_claude._login_marker(marker).write_text("not logged in\n")

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return _PANEL_2_1_283

    def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.time, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="not logged in on this host"):
        spawn_claude._wait_for_ready(7, timeout=30, failure_marker=marker)


def test_claude_command_refuses_a_signed_out_cli_before_exec(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "bin"
    path.mkdir()
    fake = path / "claude"
    fake.write_text('#!/bin/sh\n[ "$1" = auth ] && exit 1\nprintf "launched\\n"\n')
    fake.chmod(0o755)
    marker = tmp_path / "missing-claude"

    result = subprocess.run(  # noqa: S603 - isolated shell tests the fail-closed launcher command
        ["/bin/bash", "-c", spawn_claude._claude_command(tmp_path, failure_marker=marker)],
        env={"HOME": str(home), "PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 126
    assert "launched" not in result.stdout
    assert "claude is not logged in" in result.stderr
    assert spawn_claude._login_marker(marker).read_text() == "not logged in\n"
