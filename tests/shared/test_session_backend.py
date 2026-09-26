"""Tests for ``shared.session_backend`` — the cross-platform session abstraction."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from shared.platform import IS_WINDOWS
from shared.session_backend import (
    PosixProcSessionBackend,
    PtySessionBackend,
    SessionBackend,
    WinprocSessionBackend,
    get_backend,
    get_shell_backend,
)

# ---------------------------------------------------------------------------
# get_backend
# ---------------------------------------------------------------------------


def test_get_backend_returns_platform_appropriate_singleton():
    """get_backend() returns a singleton of the correct type for this platform.

    S6 step 1: on POSIX the service/daemon backend is the native
    supervisor; S7 moved the orchestration sessions onto it too.
    """
    b1 = get_backend()
    b2 = get_backend()
    assert b1 is b2  # singleton
    if IS_WINDOWS:
        assert isinstance(b1, WinprocSessionBackend)
    else:
        assert isinstance(b1, PosixProcSessionBackend)


def test_get_shell_backend_returns_platform_appropriate_singleton():
    """get_shell_backend() names the PTY backend — agent shells / watchers run
    on the self-hosted PTY supervisor (POSIX, S6 step 2) while service sessions
    live on the native supervisor.

    A distinct singleton from ``get_backend()``: the two answer different
    questions (where a service runs vs where an agent's interactive shell runs)
    and must not be collapsed into one.
    """
    b1 = get_shell_backend()
    b2 = get_shell_backend()
    assert b1 is b2  # singleton
    if IS_WINDOWS:
        from shared.windows_terminal.backend import WindowsTerminalBackend

        assert isinstance(b1, WindowsTerminalBackend)
    else:
        assert isinstance(b1, PtySessionBackend)
    assert b1 is not get_backend()


def test_get_backend_is_a_session_backend():
    """The singleton implements the SessionBackend interface."""
    backend = get_backend()
    assert isinstance(backend, SessionBackend)


def test_native_proc_dispatches_by_platform():
    """native_proc() returns the native agent-process supervisor module —
    winproc on Windows, posixproc on POSIX — with the surface agent launch /
    reap / status dispatch to."""
    from shared.session_backend import native_proc

    mod = native_proc()
    if IS_WINDOWS:
        assert mod.__name__ == "shared.winproc"
    else:
        assert mod.__name__ == "shared.posixproc"
    # the surface the consumers rely on
    for fn in ("has_session", "new_session", "kill_session", "list_sessions", "graceful_signal"):
        assert callable(getattr(mod, fn))


# ---------------------------------------------------------------------------
# WinprocSessionBackend PTY methods raise NotImplementedError
# ---------------------------------------------------------------------------


def test_winproc_send_keys_raises():
    backend = WinprocSessionBackend()
    with pytest.raises(NotImplementedError):
        backend.send_keys("sess", "key")


def test_winproc_capture_pane_raises():
    backend = WinprocSessionBackend()
    with pytest.raises(NotImplementedError):
        backend.capture_pane("sess")


@pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions require POSIX")
@pytest.mark.parametrize(
    ("relative_cwd", "projection", "expected_venv"),
    [
        ("checkout/nested", "default", "checkout"),
        ("checkout/.worktrees/task", "default", None),
        ("checkout/.claude/worktrees/task", "default", None),
        ("workspace", "default", None),
        ("workspace", "empty", None),
        ("workspace", "explicit", "explicit"),
    ],
)
def test_pty_child_virtual_env_projection(
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_cwd: str,
    projection: str,
    expected_venv: str | None,
) -> None:
    """Read a real shell's child env, including inherited-host contamination."""
    from shared import paths

    checkout = unit_home / "checkout"
    cwd = unit_home / relative_cwd
    cwd.mkdir(parents=True)
    monkeypatch.setattr(paths, "repo_root", lambda: checkout)
    # unit_home pins in-process Settings; subprocesses read the raw env at boot.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(unit_home))
    monkeypatch.setenv("HOME", str(unit_home))  # no user login-profile activation
    monkeypatch.setenv("VIRTUAL_ENV", str(unit_home / "foreign" / ".venv"))
    monkeypatch.setenv("PTY_AMBIENT_SENTINEL", "preserved")
    backend = PtySessionBackend()
    name = "ava-test-venv-projection"
    report = unit_home / "child-env.json"
    code = (
        "import json, os; from pathlib import Path; "
        f"report = Path({str(report)!r}); pending = report.with_suffix('.tmp'); "
        "pending.write_text(json.dumps("
        "{key: os.environ.get(key) for key in "
        "('VIRTUAL_ENV', 'PTY_AMBIENT_SENTINEL', 'AVA_DB_URL')})); pending.replace(report)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    env = {"VIRTUAL_ENV": "/explicit/venv", "AVA_DB_URL": "runner-override"}
    try:
        if projection == "default":
            assert backend.new_session(name, "", cwd)
        else:
            assert backend.new_session(name, "", cwd, env=env if projection == "explicit" else {})
        backend.send(name, command)
        backend.send_keys(name, "Enter")
        deadline = time.monotonic() + 15
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.exists(), backend.capture_pane(name)
        child_env = json.loads(report.read_text())
        expected = {"checkout": str(checkout / ".venv"), "explicit": "/explicit/venv", None: None}
        assert child_env["VIRTUAL_ENV"] == expected[expected_venv]
        assert child_env["PTY_AMBIENT_SENTINEL"] == "preserved"
        if projection == "explicit":
            assert child_env["AVA_DB_URL"] == "runner-override"
    finally:
        backend.kill_session(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
