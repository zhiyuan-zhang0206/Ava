"""Tests for how shared.winproc hands a session command to CreateProcess.

The defect these cover: every session ran as ``cmd /c <cmd>`` under
``DETACHED_PROCESS``, and cmd.exe — finding itself without a console — allocates
one and gives *its children* that console's handles. The daemon's stdout/stderr
then went to a throwaway console instead of the log file we opened for it, so a
Windows daemon that died at startup left nothing on disk. Measured on the fleet
Windows box: a child under ``cmd /c`` reports ``FILE_TYPE_CHAR`` handles, the
same child launched directly reports ``FILE_TYPE_DISK``.

`_plan_launch` is the decision that fixes it, and it is pure — the branch is
asserted here on any platform. The Popen dispatch test is likewise
platform-agnostic: it stubs Popen and asserts the command + creation flags that
would reach Win32, which is the part the Windows box then confirms end to end.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, ClassVar

import pytest

from shared import winproc

# ── _plan_launch: which commands avoid cmd.exe ──────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        ".venv/bin/python -m services.browser.daemon",
        ".venv/bin/python -m services.watchdog.daemon --role agent-runner",
        "npm.cmd run start",
    ],
)
def test_shell_free_command_launches_as_argv(cmd: str, tmp_path: Path) -> None:
    """A command with no cmd.exe operators goes out as an argv with no shell in
    the middle — the only shape that leaves the child holding our log handles."""
    launch = winproc._plan_launch(cmd, tmp_path)
    assert not launch.via_shell
    assert isinstance(launch.command, list)
    assert "cmd" not in launch.command
    assert launch.creationflags == winproc._DETACHED_FLAGS


@pytest.mark.parametrize(
    "cmd",
    [
        # The frontend service (gateway-only today, but the launcher is shared).
        'cd frontend && set "NEXT_PUBLIC_API=http://h:8000" && npm run build',
        # The agent-runner self-update chain (`ops.cluster` spawns this on Windows).
        'echo [updater] force-checkout & git fetch origin && git checkout -B "main" && ava restart || ava start',
        "python -m x | findstr ERROR",
        "python -m x > out.txt",
        "echo %AVA_HOME%",
    ],
)
def test_shell_command_goes_through_cmd_with_a_console(cmd: str, tmp_path: Path) -> None:
    """A command whose own syntax is cmd.exe's still needs cmd — but with
    CREATE_NO_WINDOW, so cmd starts with a console and never allocates the one
    that would swallow its children's output."""
    launch = winproc._plan_launch(cmd, tmp_path)
    assert launch.via_shell
    assert launch.command == f'cmd /s /c "{cmd}"'
    assert launch.creationflags == winproc._CMD_FLAGS
    assert launch.creationflags != winproc._DETACHED_FLAGS


def test_shell_command_keeps_its_own_quotes_verbatim(tmp_path: Path) -> None:
    """`cmd /s /c` takes the rest of the line literally after stripping the outer
    quote pair, so an inner `set "VAR=val"` survives. Passing a LIST to Popen
    instead would run it through list2cmdline, which escapes those quotes as \\"
    — two characters cmd.exe reads as a literal backslash plus a quote toggle."""
    launch = winproc._plan_launch('set "A=b" && run', tmp_path)
    assert launch.command == 'cmd /s /c "set "A=b" && run"'
    assert "\\" not in str(launch.command)


def test_argv_command_is_passed_through_untouched(tmp_path: Path) -> None:
    """The agent launcher already hands us an argv (nothing it composes may meet a
    shell); it stays an argv, detached — spaces and quotes included. An absolute
    interpreter path (what the launcher actually composes) is not a POSIX token,
    so nothing is rewritten."""
    argv = ["C:\\py\\python.exe", "-m", "agent", "--agent-id", "7", "--flag", 'a "quoted" b']
    launch = winproc._plan_launch(argv, tmp_path)
    assert launch.command == argv
    assert launch.command is not argv  # copied, not aliased
    assert launch.creationflags == winproc._DETACHED_FLAGS


def test_argv_posix_venv_token_is_rewritten(tmp_path: Path) -> None:
    """An argv element that IS the POSIX `.venv/bin/python` token must get the
    same Windows resolution as a string command — an argv is not a license to
    skip the rewrite, or the element reaches CreateProcess as a path that cannot
    exist (the '.venv' is not recognized defect class)."""
    cwd = tmp_path / "a checkout"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    argv = [".venv/bin/python", "-m", "services.browser.daemon"]
    launch = winproc._plan_launch(argv, cwd)
    assert launch.command == [str(scripts / "python.exe"), "-m", "services.browser.daemon"]
    assert launch.creationflags == winproc._DETACHED_FLAGS


def test_argv_posix_venv_token_falls_back_to_path_python(tmp_path: Path) -> None:
    """Same fallback as the string branch: no venv in the checkout → `python`."""
    launch = winproc._plan_launch([".venv/bin/python", "-m", "x"], tmp_path)
    assert launch.command == ["python", "-m", "x"]


def test_argv_mixed_tokens_only_rewrite_the_posix_one(tmp_path: Path) -> None:
    """Non-token argv elements (a resolved interpreter, flags) pass through
    untouched; only an exact `.venv/bin/python` element is rewritten."""
    cwd = tmp_path / "c"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    argv = ["C:\\ava\\python.exe", "--version"]
    launch = winproc._plan_launch(argv, cwd)
    assert launch.command == argv


# ── _plan_launch: interpreter + quoting ─────────────────────────────────────


def test_venv_python_token_is_rewritten_after_splitting(tmp_path: Path) -> None:
    """`.venv/bin/python` names the checkout's interpreter; on Windows that is
    `.venv\\Scripts\\python.exe`. The rewrite lands on the split token, so a
    checkout path containing a space stays one argv element."""
    cwd = tmp_path / "a checkout"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    launch = winproc._plan_launch(".venv/bin/python -m services.ops.daemon", cwd)
    assert launch.command == [str(scripts / "python.exe"), "-m", "services.ops.daemon"]


def test_venv_python_falls_back_to_path_python(tmp_path: Path) -> None:
    """No venv in the checkout — fall back to whatever `python` resolves to,
    matching the pre-existing contract."""
    launch = winproc._plan_launch(".venv/bin/python -m services.ops.daemon", tmp_path)
    assert launch.command == ["python", "-m", "services.ops.daemon"]


def test_venv_python_is_quoted_in_a_shell_command(tmp_path: Path) -> None:
    """In the cmd.exe branch the interpreter is spliced back into a command line,
    so a space in the checkout path has to be quoted there."""
    cwd = tmp_path / "a checkout"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    launch = winproc._plan_launch(".venv/bin/python -m x && echo done", cwd)
    assert launch.command == f'cmd /s /c ""{scripts / "python.exe"}" -m x && echo done"'


def test_quoted_argument_survives_the_split(tmp_path: Path) -> None:
    """A quoted path with a space is one argument, and the quotes are consumed by
    the split rather than passed to the program."""
    launch = winproc._plan_launch('"C:\\Program Files\\py\\python.exe" -m x', tmp_path)
    assert launch.command == ["C:\\Program Files\\py\\python.exe", "-m", "x"]


def test_unbalanced_quote_falls_back_to_cmd(tmp_path: Path) -> None:
    """Refuse to guess at a command we cannot split; cmd.exe can have it (and,
    with a console, will report its own complaint into the session log)."""
    launch = winproc._plan_launch('python -c "unterminated', tmp_path)
    assert launch.via_shell


def test_backslashes_are_path_separators_not_escapes(tmp_path: Path) -> None:
    """Windows quoting, not POSIX: the split must not eat backslashes (the
    session backend can hand us a fully resolved Windows interpreter path)."""
    launch = winproc._plan_launch(
        "C:\\ava\\.venv\\Scripts\\python.exe -m services.ops.daemon", tmp_path
    )
    assert launch.command == ["C:\\ava\\.venv\\Scripts\\python.exe", "-m", "services.ops.daemon"]


# ── new_session: what actually reaches Popen ────────────────────────────────


@dataclass(frozen=True)
class _Call:
    """The three things about a launch that decide whether output survives."""

    command: str | list[str]
    creationflags: int
    stdout: str
    stderr: str


class _FakePopen:
    """Records the Popen call and stands in for a launched process."""

    calls: ClassVar[list[_Call]] = []

    def __init__(
        self,
        command: str | list[str],
        *,
        creationflags: int,
        stdout: IO[bytes],
        stderr: IO[bytes],
        **_: object,
    ) -> None:
        _FakePopen.calls.append(_Call(command, creationflags, stdout.name, stderr.name))
        # A pid psutil can resolve, so new_session records a real create_time.
        self.pid = os.getpid()


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


# ── healthcheck respawn path (the win 2026-08-12/13 contrast case) ─────────


def test_browser_respawn_command_rewrites_on_windows(tmp_path: Path) -> None:
    """The EXACT command the browser healthcheck hands to `respawn_service`
    (`services/healthchecks/browser.py` -> `shared.service_respawn` -> the
    session backend) must resolve `.venv/bin/python` to the checkout's Windows
    interpreter and go out as an argv — the launch shape that leaves the daemon
    with the session log's handles. This is the path that kept WORKING on win
    (11:57:35 respawn, task #1235) while the session-launch failures happened
    elsewhere; it must keep working."""
    cwd = tmp_path / "source"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    cmd = ".venv/bin/python -m services.browser.daemon"
    launch = winproc._plan_launch(cmd, cwd)
    assert not launch.via_shell
    assert launch.command == [str(scripts / "python.exe"), "-m", "services.browser.daemon"]
    assert launch.creationflags == winproc._DETACHED_FLAGS


def test_frontend_respawn_command_rewrites_in_the_shell_branch(tmp_path: Path) -> None:
    """The frontend's Windows command is a `&&` chain (shell branch); the POSIX
    interpreter token inside it must still be rewritten there — the same
    contract, on the other branch."""
    cwd = tmp_path / "source"
    scripts = cwd / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")

    cmd = 'cd frontend && set "NEXT_PUBLIC_API=http://h:8000" && .venv/bin/python -m x'
    launch = winproc._plan_launch(cmd, cwd)
    assert launch.via_shell
    assert str(scripts / "python.exe") in launch.command
    assert ".venv/bin/python" not in launch.command


def test_new_session_launches_a_daemon_with_the_log_handles(
    unit_home: Path, fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    """End of the dispatch: a daemon command reaches Win32 as an argv, detached,
    with the session log as both stdout and stderr — so a startup refusal is
    written to `$AVA_HOME/logs/<name>.out.log` instead of a dead console."""
    assert winproc.new_session(
        "zz-daemon", ".venv/bin/python -m services.browser.daemon", tmp_path, env={"A": "b"}
    )
    call = fake_popen.calls[-1]
    assert call.command == ["python", "-m", "services.browser.daemon"]
    assert call.creationflags == winproc._DETACHED_FLAGS
    log = winproc.session_log_path("zz-daemon")
    assert call.stdout == str(log)
    assert call.stderr == str(log)

    rec = winproc._read_record("zz-daemon")
    assert rec is not None
    # Provenance is what actually ran, not the pre-rewrite string.
    assert rec.cmd == "python -m services.browser.daemon"


def test_new_session_splits_stderr_when_asked(
    unit_home: Path, fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    """The agent launcher splits stderr to a per-agent file; both handles are
    still real files, on the argv path."""
    stderr_log = unit_home / "logs" / "agent-7.stderr.log"
    assert winproc.new_session(
        "zz-agent", ["python", "-m", "agent"], tmp_path, env={}, stderr_append=stderr_log
    )
    call = fake_popen.calls[-1]
    assert call.stderr == str(stderr_log)
    assert call.stdout == str(winproc.session_log_path("zz-agent"))


def test_new_session_shell_command_reaches_popen_as_a_string(
    unit_home: Path, fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    """A chained command reaches Win32 as one verbatim command line with the
    console flag — the log handles we opened are the ones cmd's children get."""
    assert winproc.new_session("zz-updater", "git fetch && ava restart", tmp_path, env={})
    call = fake_popen.calls[-1]
    assert call.command == 'cmd /s /c "git fetch && ava restart"'
    assert call.creationflags == winproc._CMD_FLAGS
    assert call.stdout == str(winproc.session_log_path("zz-updater"))
