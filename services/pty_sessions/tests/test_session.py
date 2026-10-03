"""`PtySession` in isolation: the teardown claim, the transcript cap and the kill verdict.

No service and no login shell: a pipe stands in for the master, so these pin what a
running service cannot show directly (a member the caller may not signal needs a
simulated refusal, which only works inside the test process).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from base.native_process.ownership import stable_create_time
from base.sessions.pty import client
from base.sessions.record import SessionRecord, pid_starttime_ticks
from services.pty_sessions import session as session_module
from services.pty_sessions.session import PtySession
from tests.path_scoped.pty_reaper import PtyReaper
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


# A session leader with one sleeping member, the member's pid on the first output line.
_LEADER_WITH_MEMBER = (
    "import subprocess, sys, time\n"
    "member = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "print(member.pid, flush=True)\n"
    "time.sleep(300)\n"
)


def test_a_kill_with_only_unsignallable_survivors_answers_interrupted_and_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pty_reaper: PtyReaper
) -> None:
    """The shell and everything this user may signal are gone; a member it may not
    signal (a root `sudo` on the pty) survives. The session is over and its work was
    cut short: the kill answers `interrupted` with the survivor named, not an error
    that would lose the owner's interruption notice, and it does not spend its wait
    on a member nothing could signal."""
    leader = subprocess.Popen(  # noqa: S603 — the test's own interpreter and literal program
        [sys.executable, "-c", _LEADER_WITH_MEMBER],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    member = int(leader.stdout.readline())
    pty_reaper.track(psutil.Process(leader.pid), psutil.Process(member))
    read_end, write_end = os.pipe()
    record = SessionRecord(
        pid=leader.pid,
        create_time=stable_create_time(psutil.Process(leader.pid)),
        cmd="",
        cwd="",
        started_at=0.0,
        starttime=pid_starttime_ticks(leader.pid),
    )
    session = PtySession(
        "ava-test-race-1", leader.pid, read_end, 80, 24, record, tmp_path / "t.log"
    )
    real_suspend, real_kill = psutil.Process.suspend, psutil.Process.kill

    def suspend(self: psutil.Process) -> None:
        if self.pid == member:
            raise psutil.AccessDenied(self.pid)
        real_suspend(self)

    def kill(self: psutil.Process) -> None:
        if self.pid == member:
            raise psutil.AccessDenied(self.pid)
        real_kill(self)

    monkeypatch.setattr(session_module, "_KILL_FORCE_WAIT_S", 5.0)

    def reader() -> None:
        leader.wait()
        session.begin_finish()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        # A scoped patch: undone before teardown SIGKILLs the member.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(psutil.Process, "suspend", suspend)
            patch.setattr(psutil.Process, "kill", kill)
            started = time.monotonic()
            verdict = session_module.kill_session(session, graceful=False)
            elapsed = time.monotonic() - started
    finally:
        os.kill(member, signal.SIGKILL)
        os.close(write_end)
        os.close(session.master_fd)
        os.close(session._log_fd)
    thread.join(10)
    assert verdict == {"mode": "forced", "interrupted": True, "survivors": [member]}
    assert elapsed < 2.5, f"the kill waited {elapsed:.2f}s for a member it could not signal"


def test_the_client_reads_survivors_from_a_kill_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished but interrupted kill reaches the caller as a verdict, survivors named."""

    def request(method: str, **_fields: object) -> dict[str, object]:
        assert method == "kill"
        return {"mode": "forced", "interrupted": True, "survivors": [4242]}

    monkeypatch.setattr(client, "request", request)

    assert client.kill("ava-test-race-2", graceful=False) == client.KillVerdict(
        "forced", True, (4242,)
    )
