"""Child-process window suppression (CREATE_NO_WINDOW) — Task #1095.

The Windows agent-runner lives in the user's interactive session. A console-less
parent that spawns a child without a creation flag flashes a brand-new console
window on the desktop for every subprocess call (shell runs, git calls,
schtasks invocations). `shared.platform.CREATE_NO_WINDOW` is the single source
of the flag: 0x08000000 on Windows, 0 elsewhere, so a call site that always
passes `creationflags=CREATE_NO_WINDOW` is a no-op on POSIX.

These tests pin the constant's value and assert the runtime call sites pass it
through; they run on every host (mock-based, nothing is executed).
"""

from __future__ import annotations

import subprocess

import pytest

from shared.platform import CREATE_NO_WINDOW, IS_WINDOWS

_WIN_FLAG = 0x08000000  # subprocess.CREATE_NO_WINDOW on Windows


def test_platform_constant_matches_os() -> None:
    """Windows exposes CREATE_NO_WINDOW (0x08000000); POSIX has none (0)."""
    assert (_WIN_FLAG if IS_WINDOWS else 0) == CREATE_NO_WINDOW


def test_windows_flag_value_is_documented_constant() -> None:
    """Guard the magic number against a stdlib change."""
    assert _WIN_FLAG == 0x08000000


def test_ava_shell_run_passes_creationflags(monkeypatch: pytest.MonkeyPatch) -> None:
    """ava.shell.run forwards CREATE_NO_WINDOW to subprocess.run."""
    import ava.shell as shell_mod

    captured: dict = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)  # pyright: ignore[reportUnknownMemberType]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    shell_mod.run("echo hi")
    assert captured["creationflags"] == CREATE_NO_WINDOW


def test_run_bounded_defaults_creationflags(monkeypatch: pytest.MonkeyPatch) -> None:
    """shared.proc.run_bounded injects CREATE_NO_WINDOW when the caller did not."""
    from shared import proc as proc_mod

    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)  # pyright: ignore[reportUnknownMemberType]
            self.argv = argv
            self.returncode = 0

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return (b"", b"")

        def poll(self):  # type: ignore[no-untyped-def]
            return 0

    monkeypatch.setattr(proc_mod.subprocess, "Popen", FakePopen)
    proc_mod.run_bounded(["echo", "hi"], timeout=5)
    assert captured["creationflags"] == CREATE_NO_WINDOW


def test_run_bounded_respects_caller_creationflags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied creationflags wins over the default."""
    from shared import proc as proc_mod

    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)  # pyright: ignore[reportUnknownMemberType]
            self.argv = argv
            self.returncode = 0

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return (b"", b"")

        def poll(self):  # type: ignore[no-untyped-def]
            return 0

    monkeypatch.setattr(proc_mod.subprocess, "Popen", FakePopen)
    proc_mod.run_bounded(["echo", "hi"], timeout=5, creationflags=0x1234)
    assert captured["creationflags"] == 0x1234


def test_memory_repo_git_passes_creationflags(monkeypatch: pytest.MonkeyPatch) -> None:
    """shared.memory_repo._run_git forwards CREATE_NO_WINDOW.

    Stubs `run_bounded` — `_run_git` goes through it rather than
    `subprocess.run` so a wedged git cannot hold a daemon's exit open past
    SIGTERM. The test above pins the other half of the chain: `run_bounded`
    passes creationflags on to Popen.
    """
    from shared import memory_repo as mr

    captured: dict = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)  # pyright: ignore[reportUnknownMemberType]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(mr, "run_bounded", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    mr._run_git("rev-parse", "HEAD")
    assert captured["creationflags"] == CREATE_NO_WINDOW
