"""A PID enumeration is not an atomic snapshot of a forking POSIX session."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.sessions.pty import session_tree
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="POSIX sessions")


def test_a_fork_handoff_between_pid_enumeration_and_session_reads_is_not_empty(
    tmp_path: Path, pty_reaper: PtyReaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "handoff.py"
    script.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "home=Path(sys.argv[1])\n"
        "(home/'ready').touch()\n"
        "while not (home/'fork').exists(): time.sleep(.001)\n"
        "if os.fork()!=0: os._exit(0)\n"
        "(home/'child.tmp').write_text(str(os.getpid()))\n"
        "(home/'child.tmp').replace(home/'child')\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    leader = subprocess.Popen(  # noqa: S603 — the test's own interpreter and temporary script
        [sys.executable, str(script), str(tmp_path)], start_new_session=True
    )
    pty_reaper.track(psutil.Process(leader.pid))
    deadline = time.monotonic() + 10
    while not (tmp_path / "ready").exists():
        assert time.monotonic() < deadline, "the handoff leader did not start"
        time.sleep(0.01)
    # Preserve the real parent-table pass. Only the subsequent PID census is
    # held across a real fork/reap handoff; no process rows are fabricated.
    parents = list(psutil.process_iter(["ppid"]))
    real_pids = psutil.pids
    first = True

    def parent_snapshot(*_args: object, **_kwargs: object) -> Iterator[psutil.Process]:
        return iter(parents)

    def pids() -> list[int]:
        nonlocal first
        snapshot = real_pids()
        if first:
            first = False
            (tmp_path / "fork").touch()
            leader.wait(timeout=10)
            while not (tmp_path / "child").exists():
                assert time.monotonic() < deadline, "the successor did not publish its PID"
                time.sleep(0.001)
            successor = int((tmp_path / "child").read_text())
            assert successor not in snapshot
            pty_reaper.track(psutil.Process(successor))
        return snapshot

    with monkeypatch.context() as patch:
        patch.setattr(psutil, "process_iter", parent_snapshot)
        patch.setattr(psutil, "pids", pids)
        table = session_tree._scan()

    successor = int((tmp_path / "child").read_text())
    assert table.sessions.get(successor) == leader.pid, "the census certified a live session empty"
    assert table.complete


def test_an_exhausted_census_cannot_certify_an_empty_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    next_pid = 1_000_000

    def pids() -> list[int]:
        nonlocal next_pid
        next_pid += 1
        return [next_pid]

    def empty_parents(*_args: object, **_kwargs: object) -> Iterator[psutil.Process]:
        return iter(())

    monkeypatch.setattr(session_tree, "_MAX_CENSUS_PASSES", 2)
    monkeypatch.setattr(psutil, "process_iter", empty_parents)
    monkeypatch.setattr(psutil, "pids", pids)
    # These rows deliberately cannot resolve: the budget case proves no
    # process is owned or signalled, only that unread births prevent success.
    table = session_tree._scan()
    assert not table.complete
    assert table.sessions == {}
    shell = session_tree.OwnedProcess(999_999, 1.0, None)
    capture = session_tree.SessionCapture(shell, [], None)
    monkeypatch.setattr(session_tree, "_scan", lambda: table)
    assert session_tree.refresh([capture])


def test_an_unverified_freeze_closure_fails_instead_of_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = session_tree.OwnedProcess(999_999, 1.0, None)
    table = session_tree._Table({}, {}, time.monotonic(), False)
    monkeypatch.setattr(session_tree, "_MAX_FREEZE_PASSES", 2)
    monkeypatch.setattr(session_tree, "_scan", lambda: table)

    with pytest.raises(
        RuntimeError, match=r"PID census incomplete.*membership could not be verified"
    ):
        session_tree.kill_session_tree(shell, wait_s=0.1)
