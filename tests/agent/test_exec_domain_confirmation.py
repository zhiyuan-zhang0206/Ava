"""Positive managed-domain closure, separate from signal submission or root exit."""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import psutil
import pytest

from agent.graph import _exec_process
from shared.platform import IS_WINDOWS
from shared.winjob import WindowsJob


def _ended(identity: psutil.Process) -> bool:
    try:
        return identity.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        return True


def test_real_domain_confirms_grandchild_with_redirected_output(tmp_path: Path) -> None:
    """An already-exited root and EOF alone do not certify its living member."""
    gate = tmp_path / "gate"
    receipt = tmp_path / "member"
    code = """
import os,pathlib,subprocess,sys,time
gate,receipt=map(pathlib.Path,sys.argv[1:])
until=time.monotonic()+10
while not gate.exists():
    if time.monotonic()>until: raise RuntimeError('fixture attach expired')
    time.sleep(.01)
p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)'],
                   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
receipt.write_text(str(p.pid))
os._exit(0)
"""
    job = WindowsJob.create() if IS_WINDOWS else None
    root = subprocess.Popen(  # noqa: S603 -- fixed disposable native CI fixture, no shell.
        [sys.executable, "-I", "-c", code, str(gate), str(receipt)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=not IS_WINDOWS,
    )
    member = None
    try:
        if job is not None:
            job.assign(root)
        identity = psutil.Process(root.pid)
        gate.write_text("attached")
        until = time.monotonic() + 10
        while not receipt.exists() or not _ended(identity):
            if time.monotonic() > until:
                raise AssertionError("fixture root did not exit without reap")
            time.sleep(0.01)
        member = psutil.Process(int(receipt.read_text()))
        assert not _ended(member)
        domain = _exec_process.ExecProcessDomain(root, job)
        domain.close_confirmed(time.monotonic() + 5)
        assert _ended(member)
        assert root.wait(timeout=5) == 0
        if job is not None:
            assert job.closed
    finally:
        if job is not None and not job.closed:
            job.close()
        if root.returncode is None:
            root.kill()
            root.wait(timeout=5)
        if member is not None and not _ended(member):
            member.kill()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX group observation contract")
def test_live_group_after_signal_is_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Root:
        pid = 123

        def kill(self) -> None:
            raise AssertionError("unexpected direct kill")

    def live_group(_pid: int) -> bool:
        return True

    def signal_submitted(_pid: int, _sig: int) -> None:
        pass

    monkeypatch.setattr("shared.exec_process_domain._process_group_has_live_member", live_group)
    monkeypatch.setattr(os, "killpg", signal_submitted)
    domain = _exec_process.ExecProcessDomain(cast(subprocess.Popen[bytes], Root()), None)
    with pytest.raises(TimeoutError, match="live managed members"):
        domain.close_confirmed(time.monotonic() - 1)
