"""Graceful-signal routing across the Windows session boundary (issue #1930).

`AttachConsole` cannot cross a session boundary, so `winproc.graceful_signal`
routes by session: same-session targets keep the direct one-shot private-console
helper; cross-session targets go through the resident control steward whose
socket path the session record binds to its exact (pid, create_time) identity.
Every refusal below must be loud (raise) and never escalate to force — the stop
flow reports an incomplete stop and the operator decides.

Windows-only logic, asserted on any platform: the routing is a pure decision
over mocked session ids, socket, and psutil state, per the winproc test
conventions.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar

import pytest

from shared import winproc
from shared.session_record import SessionRecord


def _record(*, steward: bool = True, pid: int = 123) -> SessionRecord:
    return SessionRecord(
        pid,
        5.0,
        "fixture",
        "/private-test",
        5.0,
        control_mode="private-console-v1",
        steward_pid=999 if steward else None,
        steward_socket="/run/ctrl/ava-ctrl-123-5.000000.sock" if steward else None,
    )


@pytest.fixture
def record(monkeypatch: pytest.MonkeyPatch) -> SessionRecord:
    value = _record()
    monkeypatch.setattr(winproc, "_read_record", _returning(value))
    monkeypatch.setattr(
        winproc,
        "_process_for_record",
        _returning(SimpleNamespace(create_time=lambda: 5.0)),
    )
    monkeypatch.setattr(winproc.time, "monotonic", _returning(100.0))
    return value


def _returning(value: object) -> object:
    """A typed stand-in for monkeypatch lambdas (pyright-friendly)."""

    def _answer(*_args: object, **_kwargs: object) -> object:
        return value

    return _answer


def forbidden(*_args: object, **_kwargs: object) -> object:
    pytest.fail("unexpected helper call")


class _FakeSocket:
    """A socket stand-in recording the steward exchange."""

    sent: ClassVar[list[bytes]] = []
    reply: ClassVar[bytes] = b"ok\n"
    connect_errors: ClassVar[type[Exception] | None] = None
    timeout_after_send: ClassVar[bool] = False

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self._timeout = timeout

    def connect(self, path: str) -> None:
        if _FakeSocket.connect_errors is not None:
            raise _FakeSocket.connect_errors
        assert path == "/run/ctrl/ava-ctrl-123-5.000000.sock"

    def sendall(self, data: bytes) -> None:
        _FakeSocket.sent.append(data)
        if _FakeSocket.timeout_after_send:
            raise TimeoutError

    def recv(self, size: int) -> bytes:
        return _FakeSocket.reply

    def close(self) -> None:
        pass


@pytest.fixture
def fake_socket(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSocket]:
    _FakeSocket.sent = []
    _FakeSocket.reply = b"ok\n"
    _FakeSocket.connect_errors = None
    _FakeSocket.timeout_after_send = False
    monkeypatch.setattr(winproc.socket, "socket", _FakeSocket)
    return _FakeSocket


def _session_of(_pid: int) -> int:
    return 1


def _caller_session() -> int:
    return 0


def _caller_session_same() -> int:
    return 1


def _same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "process_session_id", _session_of)
    monkeypatch.setattr(winproc, "current_session_id", _caller_session_same)


def _cross_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "process_session_id", _session_of)
    monkeypatch.setattr(winproc, "current_session_id", _caller_session)


# ── same session: the direct helper path is untouched ───────────────────────


def test_same_session_uses_the_direct_helper(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    _same_session(monkeypatch)
    calls: list[list[str]] = []

    def helper(args: list[str], *, timeout: float) -> SimpleNamespace:
        calls.append(args)
        assert timeout > 0
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(winproc, "run_job_process", helper)
    monkeypatch.setattr(winproc, "socket", forbidden)
    assert winproc.graceful_signal("service", expected=record, timeout=0.2)
    assert calls[0][-3:] == ["123", "5.0", "100.2"]


# ── cross session: the steward channel ──────────────────────────────────────


def test_cross_session_delivers_through_the_steward(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert winproc.graceful_signal("service", expected=record, timeout=5.0)
    assert fake_socket.sent == [b"break\n"]


def test_cross_session_legacy_record_refuses_without_steward(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record from before the steward existed cannot be reached across the
    session boundary — the refusal says what to do, and nothing is signalled."""
    _cross_session(monkeypatch)
    legacy = _record(steward=False)
    monkeypatch.setattr(winproc, "_read_record", _returning(legacy))
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(RuntimeError, match="predates the resident control channel"):
        winproc.graceful_signal("service", expected=legacy, timeout=5.0)
    assert fake_socket.sent == []


def test_cross_session_steward_gone_refuses(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    fake_socket.connect_errors = ConnectionRefusedError
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(RuntimeError, match="steward for service is not reachable"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_cross_session_steward_refusal_is_reported(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    fake_socket.reply = b"err: private console record identity changed or is unproven\n"
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(RuntimeError, match=r"refused delivery.*identity changed"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_cross_session_steward_timeout_is_a_timeout_error(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    fake_socket.timeout_after_send = True
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(TimeoutError, match="did not answer"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_cross_session_reply_after_deadline_is_a_timeout_error(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    ticks = iter([100.0, 101.0, 106.0])  # deadline, remaining, after recv
    monkeypatch.setattr(winproc.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(TimeoutError, match="answered after the graceful delivery deadline"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_cross_session_deadline_passed_before_connect(
    record: SessionRecord, fake_socket: type[_FakeSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cross_session(monkeypatch)
    ticks = iter([100.0, 106.0])  # deadline, remaining
    monkeypatch.setattr(winproc.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="deadline passed"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


# ── identity uncertainty refuses; a vanished target is not an error ─────────


def test_target_session_unknown_means_gone(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProcessIdToSessionId failing on the target means it exited mid-check —
    the same 'already gone' the caller's wait treats as success."""

    def _unknown_session(_pid: int) -> None:
        return None

    monkeypatch.setattr(winproc, "process_session_id", _unknown_session)
    monkeypatch.setattr(winproc, "current_session_id", _caller_session)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert not winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_caller_session_unknown_refuses(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unknown_caller() -> None:
        return None

    monkeypatch.setattr(winproc, "process_session_id", _session_of)
    monkeypatch.setattr(winproc, "current_session_id", _unknown_caller)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    with pytest.raises(RuntimeError, match="caller's own session"):
        winproc.graceful_signal("service", expected=record, timeout=5.0)


def test_expected_identity_still_gates_before_any_routing(
    record: SessionRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "process_session_id", forbidden)
    monkeypatch.setattr(winproc, "current_session_id", forbidden)
    monkeypatch.setattr(winproc, "run_job_process", forbidden)
    assert not winproc.graceful_signal("service", expected=replace(record, pid=124), timeout=5.0)
