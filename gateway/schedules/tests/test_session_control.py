"""The API's reach into the schedule sessions: log capture reads the shell backend."""

from __future__ import annotations

import pytest

from base.sessions.backend import get_shell_backend
from gateway.schedules import session_control


class _Backend:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def has_session(self, name: str) -> bool:
        return name in self.live

    def capture_pane(self, name: str, lines: int) -> str:
        return f"{name}:{lines}"


def test_capture_returns_none_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_control, "get_shell_backend", lambda: _Backend(set()))

    assert session_control.capture_blocking(123, 200) is None


def test_capture_reads_the_live_sessions_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_control, "get_shell_backend", lambda: _Backend({"ava-schedule-123"})
    )

    assert session_control.capture_blocking(123, 50) == "ava-schedule-123:50"


def test_capture_uses_the_shell_backend() -> None:
    """Schedule sessions are PTY-supervisor sessions, the same backend the service
    launches them on; the service backend must never see them."""
    assert session_control.get_shell_backend is get_shell_backend
