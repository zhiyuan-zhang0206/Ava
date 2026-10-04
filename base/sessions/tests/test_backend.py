"""Tests for ``base.sessions.backend`` session selection."""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from base.sessions.backend import (
    PosixProcSessionBackend,
    PtySessionBackend,
    SessionBackend,
    get_backend,
    get_shell_backend,
)
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service

# ---------------------------------------------------------------------------
# get_backend
# ---------------------------------------------------------------------------


def test_get_backend_returns_platform_appropriate_backend():
    """get_backend() returns the correct type for this platform.

    S6 step 1: on POSIX the service/daemon backend is the native
    supervisor; S7 moved the orchestration sessions onto it too.
    """
    assert isinstance(get_backend(), PosixProcSessionBackend)


def test_get_shell_backend_returns_platform_appropriate_backend():
    """get_shell_backend() names the PTY backend — agent shells / watchers run
    on the self-hosted PTY supervisor (POSIX, S6 step 2) while service sessions
    live on the native supervisor.

    A distinct backend from ``get_backend()``: the two answer different
    questions (where a service runs vs where an agent's interactive shell runs)
    and must not be collapsed into one.
    """
    shell = get_shell_backend()
    assert isinstance(shell, PtySessionBackend)
    assert not isinstance(get_backend(), PtySessionBackend)


def test_get_backend_is_a_session_backend():
    """The backend implements the SessionBackend interface."""
    backend = get_backend()
    assert isinstance(backend, SessionBackend)


def test_native_proc_dispatches_by_platform():
    """native_proc() returns the native agent-process supervisor module —
    posixproc with the surface agent launch / reap / status dispatch to."""
    from base.sessions.backend import native_proc

    mod = native_proc()
    assert mod.__name__ == "base.sessions.posixproc"
    # the surface the consumers rely on
    for fn in ("has_session", "new_session", "kill_session", "list_sessions", "graceful_signal"):
        assert callable(getattr(mod, fn))


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
    pty_service: PtyServiceProcess,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_cwd: str,
    projection: str,
    expected_venv: str | None,
) -> None:
    """Read a real shell's child env from the pty-sessions service, including a
    service environment that carries a foreign virtualenv.

    The shell's base is the service's environment (its ambient variables reach the
    shell, its `VIRTUAL_ENV` does not); the virtualenv comes only from the
    creator's projection or an explicit entry."""
    from base import paths

    checkout = unit_home / "checkout"
    cwd = unit_home / relative_cwd
    cwd.mkdir(parents=True)
    monkeypatch.setattr(paths, "repo_root", lambda: checkout)
    # The service environment is the shell's base: restart the service with an
    # ambient sentinel and a foreign virtualenv in it.
    pty_service.stop()
    service = PtyServiceProcess(
        unit_home,
        {
            "VIRTUAL_ENV": str(unit_home / "foreign" / ".venv"),
            "PTY_AMBIENT_SENTINEL": "preserved",
        },
    )
    service.start()
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
        service.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
