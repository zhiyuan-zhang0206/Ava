"""Positive managed-domain closure, separate from signal submission or root exit."""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import cast

import psutil
import pytest

from agent.graph import _exec_process
from shared.platform import IS_WINDOWS
from shared.winjob import WindowsJob, _kernel32
from shared.winjob_pipes import PipedJobChild, start_piped_job_process


def _belongs_to_job(job: WindowsJob, pid: int) -> bool:
    """Read this exact Job, not merely membership in the CI runner's Job."""
    api = _kernel32()
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    handle = api.OpenProcess(0x1000, False, pid)
    assert handle, "cannot inspect the actual fixture process"
    try:
        member = wintypes.BOOL()
        assert api.IsProcessInJob(handle, job.handle, ctypes.byref(member))
        return bool(member.value)
    finally:
        assert api.CloseHandle(handle)


def _ended(identity: psutil.Process) -> bool:
    try:
        if IS_WINDOWS:
            # Windows status() is not a native process-handle termination wait.
            identity.wait(timeout=0)
            return True
        return identity.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.TimeoutExpired:
        return False
    except psutil.NoSuchProcess:
        return True


def _close_fixture(
    root: subprocess.Popen[bytes] | PipedJobChild,
    job: WindowsJob | None,
    member: psutil.Process | None,
) -> None:
    if job is not None and not job.closed:
        job.close()
    if root.returncode is None:
        root.kill()
        root.wait(timeout=5)
    if member is not None and not _ended(member):
        member.kill()
        member.wait(timeout=5)
    if root.stdin is not None:
        root.stdin.close()
    if root.stdout is not None:
        root.stdout.close()


def test_real_domain_confirms_grandchild_with_redirected_output(tmp_path: Path) -> None:
    _exercise_domain(tmp_path, late_attach=False)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows venv redirector attachment boundary")
def test_late_job_attach_does_not_adopt_existing_interpreter(tmp_path: Path) -> None:
    _exercise_domain(tmp_path, late_attach=True)


def _exercise_domain(tmp_path: Path, *, late_attach: bool) -> None:
    """An already-exited root and EOF alone do not certify its living member."""
    gate = tmp_path / "gate"
    receipt = tmp_path / "member"
    interpreter = tmp_path / "interpreter"
    code = """
import os,pathlib,subprocess,sys,time
gate,receipt,interpreter=map(pathlib.Path,sys.argv[1:])
interpreter.write_text(str(os.getpid()))
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
    argv = [sys.executable, "-I", "-c", code, str(gate), str(receipt), str(interpreter)]
    root = (
        start_piped_job_process(argv, job)
        if job is not None and not late_attach
        else subprocess.Popen(  # noqa: S603 -- fixed disposable native CI fixture, no shell.
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=not IS_WINDOWS,
        )
    )
    member = None
    try:
        until = time.monotonic() + 10
        while not interpreter.exists():
            if time.monotonic() >= until:
                raise AssertionError("actual interpreter did not start")
            time.sleep(0.01)
        interpreter_pid = int(interpreter.read_text())
        if job is not None and late_attach:
            assert isinstance(root, subprocess.Popen)
            assert interpreter_pid != root.pid, "negative requires actual Windows venv redirector"
            job.assign(root)
        if job is not None:
            assert _belongs_to_job(job, interpreter_pid) is not late_attach
        identity = psutil.Process(root.pid)
        gate.write_text("attached")
        until = time.monotonic() + 10
        while not receipt.exists() or not _ended(identity):
            if time.monotonic() > until:
                raise AssertionError("fixture root did not exit without reap")
            time.sleep(0.01)
        member = psutil.Process(int(receipt.read_text()))
        member_birth = member.create_time()
        assert not _ended(member)
        if job is not None:
            assert _belongs_to_job(job, member.pid) is not late_attach
            assert member.create_time() == member_birth
        domain = _exec_process.ExecProcessDomain(root, job)
        domain.close_confirmed(time.monotonic() + 5)
        # Late attachment closes an empty Job, not the escaped fixture child.
        # This negative demonstrates why only atomic creation supports closure.
        assert _ended(member) is not late_attach
        assert root.wait(timeout=5) == 0
        if job is not None:
            assert job.closed
    finally:
        _close_fixture(root, job, member)


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
