"""`PtySession` in isolation: the teardown claim, the transcript cap and the kill verdict.

No service and no login shell: a pipe stands in for the master, so these pin what a
running service cannot show directly (a member the caller may not signal needs a
simulated refusal, which only works inside the test process).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import psutil
import pytest

from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client
from base.sessions.record import SessionRecord
from services.agent_runner.pty_sessions import session as session_module
from services.agent_runner.pty_sessions.service import PtyService
from services.agent_runner.pty_sessions.session import PtySession
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper

MakeSession = Callable[[str, int], PtySession]


@pytest.fixture
def make_session(tmp_path: Path) -> Iterator[MakeSession]:
    """A factory of sessions on a pipe master; every fd is closed afterwards."""
    sessions: list[PtySession] = []

    def make(name: str, log_cap: int) -> PtySession:
        read_end, write_end = os.pipe()
        os.close(write_end)
        record = SessionRecord(pid=os.getpid(), create_time=0.0, cmd="", cwd="", started_at=0.0)
        session = PtySession(
            name,
            os.getpid(),
            read_end,
            120,
            40,
            record,
            tmp_path / f"{name}.out.log",
            log_cap=log_cap,
        )
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        os.close(session.master_fd)
        os.close(session._log_fd)


def test_begin_finish_has_single_winner(make_session: MakeSession) -> None:
    """Concurrent teardown claims must have exactly one winner: a double
    winner would double-close the master fd."""
    session = make_session("ava-test-win-1", 1024)
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(session.begin_finish())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1, results
    assert session.dead
    assert not session.begin_finish(), "later claims must lose"


def test_transcript_log_is_capped(make_session: MakeSession, tmp_path: Path) -> None:
    """The byte transcript stops growing at the per-session cap: past the cap
    the service keeps the session but stops appending."""
    session = make_session("ava-test-logcap-1", 200)
    log_file = tmp_path / "ava-test-logcap-1.out.log"
    header = log_file.read_bytes()
    assert header.startswith(b"--- ava session ava-test-logcap-1 start=")
    assert header.endswith(f" pid={session.pid} ---\n".encode())
    assert session._log_written == len(header)
    session.log_write(b"a" * 60)
    session.log_write(b"b" * 200)
    assert len(log_file.read_bytes()) == 200
    assert log_file.read_bytes() == header + b"a" * 60 + b"b" * (140 - len(header))
    session.log_write(b"c" * 50)
    assert session._log_written == 200
    assert len(log_file.read_bytes()) == 200, "log must not grow past the cap"


def _live(_identity: OwnedProcess) -> bool:
    return True


def _no_signal(_identities: Iterable[OwnedProcess], _signum: int) -> None:
    pass


def _still_waiting(identities: Iterable[OwnedProcess], _timeout: float) -> tuple[OwnedProcess, ...]:
    return tuple(identities)


def test_a_concrete_group_signal_error_is_not_a_success(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-denied", 1024)
    monkeypatch.setattr(session_module.process_groups, "live", _live)

    def denied(*_args: object) -> None:
        raise PermissionError("known group signal denied")

    monkeypatch.setattr(session_module.process_groups, "signal", denied)
    with pytest.raises(PermissionError, match="known group signal denied"):
        session_module.kill_session(session, graceful=False, interrupted=True)


def test_a_live_shell_cannot_be_reported_closed(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-live", 1024)
    monkeypatch.setattr(session_module.process_groups, "live", _live)
    monkeypatch.setattr(session_module.process_groups, "signal", _no_signal)
    monkeypatch.setattr(session_module.process_groups, "wait", _still_waiting)
    with pytest.raises(RuntimeError, match="shell survived"):
        session_module.kill_session(session, graceful=False, interrupted=False)


def test_unknown_foreground_is_interrupted_without_discovering_processes(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-unknown", 1024)
    monkeypatch.setattr(session_module.OwnedProcess, "live", _live)

    def unavailable(_fd: int) -> int:
        raise OSError("foreground unavailable")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup must not enumerate or freeze processes")

    session.known_groups = (
        session.shell,
    )  # an earlier observation must not survive unknown foreground
    monkeypatch.setattr(os, "tcgetpgrp", unavailable)
    monkeypatch.setattr(psutil, "process_iter", forbidden)
    monkeypatch.setattr(psutil, "pids", forbidden)
    monkeypatch.setattr(psutil.Process, "children", forbidden)
    monkeypatch.setattr(psutil.Process, "suspend", forbidden)
    assert session.capture_foreground() is True
    assert session.known_groups == ()


def test_foreground_in_another_session_is_not_adopted(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-wrong-session", 1024)
    monkeypatch.setattr(session_module.OwnedProcess, "live", _live)

    def own_pid(_fd: int) -> int:
        return os.getpid()

    def another_session(_pid: int) -> int:
        return session.pid + 1

    monkeypatch.setattr(os, "tcgetpgrp", own_pid)
    session.pid = os.getpid() + 1
    monkeypatch.setattr(os, "getpgid", own_pid)
    monkeypatch.setattr(os, "getsid", another_session)
    assert session.capture_foreground() is True
    assert session.known_groups == ()


def test_master_close_failure_is_visible_after_the_teardown_claim(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-close-error", 1024)

    def no_reap(_session: PtySession) -> None:
        pass

    monkeypatch.setattr(session_module, "reap_child", no_reap)
    real_close = os.close

    def close(fd: int) -> None:
        if fd == session.master_fd:
            raise OSError("master close failed")
        real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close)
        with pytest.raises(OSError, match="master close failed"):
            session_module.finish(session, lambda _session: None)
    with pytest.raises(OSError, match="master close failed"):
        session.wait_dead(0)
    # finish already closed the transcript; keep the factory cleanup valid.
    session._log_fd = os.open(os.devnull, os.O_WRONLY)


def test_reap_and_callback_errors_still_close_the_master(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-reap-error", 1024)

    def failed_reap(_session: PtySession) -> None:
        raise RuntimeError("reap failed")

    def failed_callback(_session: PtySession) -> None:
        raise RuntimeError("callback failed")

    monkeypatch.setattr(session_module, "reap_child", failed_reap)
    with pytest.raises(RuntimeError, match="callback failed"):
        session_module.finish(session, failed_callback)
    with pytest.raises(OSError):
        os.fstat(session.master_fd)
    with pytest.raises(RuntimeError, match="callback failed"):
        session.wait_dead(0)
    session.master_fd = os.open(os.devnull, os.O_RDONLY)
    session._log_fd = os.open(os.devnull, os.O_WRONLY)


def test_closure_preparation_error_releases_the_allocation_fence(
    make_session: MakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("ava-test-preparation-error", 1024)
    service = PtyService()
    service._sessions[session.name] = session

    def failed_capture() -> bool:
        raise PermissionError("foreground preparation failed")

    monkeypatch.setattr(session, "capture_foreground", failed_capture)
    with pytest.raises(PermissionError, match="foreground preparation failed"):
        service.close_everything(grace_s=0, kill_s=0)
    assert service._closing is False


def test_the_client_reads_survivors_from_a_kill_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished but interrupted kill reaches the caller as a verdict, survivors named."""

    def request(method: str, **_fields: object) -> dict[str, object]:
        assert method == "kill"
        return {"mode": "forced", "interrupted": True, "survivors": [4242]}

    monkeypatch.setattr(client, "request", request)

    assert client.kill("ava-test-race-2", graceful=False) == client.KillVerdict(
        "forced", True, (4242,)
    )
