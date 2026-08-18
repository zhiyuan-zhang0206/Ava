"""Tests for ``shared.session_backend`` — the cross-platform session abstraction."""

from __future__ import annotations

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
        assert isinstance(b1, WinprocSessionBackend)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
