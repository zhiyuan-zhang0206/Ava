"""Session-identity helpers — the decision inputs for graceful routing.

The Win32 calls are mocked; the logic asserted here is the mapping between the
OS answers and what the callers do with them: an unknown session must read as
"refuse", never as a guess, and `0xFFFFFFFF` from WTSGetActiveConsoleSessionId
means "no interactive session".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shared import windows_session


def test_helpers_are_windows_only() -> None:
    with pytest.raises(RuntimeError, match="Windows-only"):
        windows_session.process_session_id(123)


def _identity(obj: Any) -> Any:
    """Stand-in for `ctypes.byref` so the fake kernel gets the DWORD itself."""
    return obj


def _kernel_answer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_by_pid: dict[int, int],
    active: int,
    current_pid: int = 4242,
) -> None:
    def process_to_session(pid: int, out: Any) -> int:
        session = session_by_pid.get(pid)
        if session is None:
            return 0  # FALSE: the process is gone / unreadable
        out.value = session
        return 1

    monkeypatch.setattr(
        windows_session,
        "_kernel32",
        lambda: SimpleNamespace(
            ProcessIdToSessionId=process_to_session,
            WTSGetActiveConsoleSessionId=lambda: active,
            GetCurrentProcessId=lambda: current_pid,
        ),
    )
    monkeypatch.setattr(windows_session.ctypes, "byref", _identity)


def test_process_session_id_reads_the_os_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_answer(
        monkeypatch,
        session_by_pid={77: 3},
        active=windows_session._NO_ACTIVE_SESSION,
    )
    assert windows_session.process_session_id(77) == 3


def test_process_session_id_unknown_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_answer(
        monkeypatch,
        session_by_pid={},
        active=windows_session._NO_ACTIVE_SESSION,
    )
    assert windows_session.process_session_id(99) is None


def test_current_session_id_maps_its_own_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_answer(
        monkeypatch,
        session_by_pid={4242: 5},
        active=windows_session._NO_ACTIVE_SESSION,
    )
    assert windows_session.current_session_id() == 5


def test_no_active_console_session_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_answer(
        monkeypatch,
        session_by_pid={},
        active=windows_session._NO_ACTIVE_SESSION,
    )
    assert windows_session.active_console_session_id() is None


def test_active_console_session_id_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _kernel_answer(monkeypatch, session_by_pid={}, active=1)
    assert windows_session.active_console_session_id() == 1
