"""`capture_shell` trims blank padding from the captured tail.

A cursor-addressed TUI's pyte scrollback is dominated by blank rows (each
full-screen redraw scrolls cleared rows into the history), so a plain tail can
open with dozens of empty lines — the shell-monitor page rendered those as a
huge blank region above the real output. Trailing blank screen rows below the
last line of a short session did the mirror-image damage (the bottom-anchored
pane scrolled the real output above the fold). Blank padding at the extremes
is never meaningful output; interleaved blanks are preserved.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ops.cluster_status import capture_shell
from ops.rpc_schemas import ShellInfo


class _FakeBackend:
    def __init__(self, captured: str) -> None:
        self._captured = captured
        self.calls: list[tuple[str, int]] = []

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        self.calls.append((name, lines))
        return self._captured


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], _FakeBackend]:
    def _install(captured: str) -> _FakeBackend:
        backend = _FakeBackend(captured)
        monkeypatch.setattr("shared.session_backend._shell_backend", backend)
        return backend

    return _install


@pytest.fixture
def shell_sessions(monkeypatch: pytest.MonkeyPatch) -> Callable[[ShellInfo], None]:
    def _install(shell: ShellInfo) -> None:
        monkeypatch.setattr(
            "ops.cluster_status.agent_shell_sessions",
            lambda _agent_id: [shell],  # pyright: ignore[reportUnknownArgumentType]
        )

    return _install


def test_capture_trims_leading_and_trailing_blank_padding(
    fake_backend: Callable[[str], _FakeBackend],
    shell_sessions: Callable[[ShellInfo], None],
) -> None:
    shell_sessions(ShellInfo(id=7))
    backend = fake_backend("\n\n\nreal line one\nreal line two\n\nreal line three\n\n\n")

    name, lines = capture_shell(42, 7, lines=200)

    assert lines == ["real line one", "real line two", "", "real line three"]
    assert backend.calls == [(name, 200)]
    assert name.endswith("-agent-42-shell-7")


def test_capture_of_all_blank_output_is_empty(
    fake_backend: Callable[[str], _FakeBackend],
    shell_sessions: Callable[[ShellInfo], None],
) -> None:
    """A session that has produced no real output yet must not render as a
    wall of blank lines — the monitor page shows its "(no output)" state."""
    shell_sessions(ShellInfo(id=3))
    fake_backend("\n\n\n")

    _, lines = capture_shell(42, 3, lines=200)

    assert lines == []


def test_capture_without_blank_padding_is_unchanged(
    fake_backend: Callable[[str], _FakeBackend],
    shell_sessions: Callable[[ShellInfo], None],
) -> None:
    """A plain log tail (the normal case) passes through byte-for-byte."""
    shell_sessions(ShellInfo(id=9))
    fake_backend("line one\nline two\nline three\n")

    _, lines = capture_shell(42, 9, lines=200)

    assert lines == ["line one", "line two", "line three"]
