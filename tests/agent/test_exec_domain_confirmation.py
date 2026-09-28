"""Positive managed-domain closure, separate from signal submission or root exit."""

import contextlib
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil
import pytest

from agent.graph import _exec_process
from shared import process_group_closure
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


def _spawn_fixture(
    argv: list[str],
    job: WindowsJob | None,
    *,
    late_attach: bool,
) -> tuple[subprocess.Popen[bytes] | PipedJobChild, _exec_process.ExecProcessDomain]:
    if not IS_WINDOWS:
        root, domain = _exec_process.ExecProcessDomain.launch_posix(
            argv,
            new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        root = (
            start_piped_job_process(argv, job)
            if job is not None and not late_attach
            else subprocess.Popen(  # noqa: S603 -- private native fixture.
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        )
        domain = _exec_process.ExecProcessDomain(root, job)
    return root, domain


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
    root, domain = _spawn_fixture(argv, job, late_attach=late_attach)
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
        close_deadline = time.monotonic() + 5
        domain.close_confirmed(close_deadline)
        while time.monotonic() < close_deadline and not _ended(member):
            time.sleep(0.01)
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
    process, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    original_signal = os.killpg
    try:

        def submitted(_pid: int, _sig: int) -> None:
            return None

        monkeypatch.setattr(os, "killpg", submitted)
        with pytest.raises(TimeoutError, match="still live after its group signal"):
            domain.close_confirmed(time.monotonic() - 1)
        assert process.returncode is None
    finally:
        original_signal(process.pid, 9)
        process.wait(timeout=5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_fast_exit_owner_closes_before_reap() -> None:
    for _ in range(8):
        root, domain = _exec_process.ExecProcessDomain.launch_posix(
            [sys.executable, "-I", "-c", "pass"]
        )
        try:
            deadline = time.monotonic() + 5
            while not _ended(psutil.Process(root.pid)) and time.monotonic() < deadline:
                time.sleep(0.005)
            assert root.returncode is None
            domain.close_confirmed(deadline)
            assert root.returncode is None
            assert root.wait(timeout=5) == 0
        finally:
            if root.returncode is None:
                root.kill()
                root.wait(timeout=5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_reaped_owner_cannot_signal_a_reused_group(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "pass"]
    )
    root.wait(timeout=5)
    signals: list[int] = []

    def recorded(pid: int, _signum: int) -> None:
        signals.append(pid)

    monkeypatch.setattr(os, "killpg", recorded)
    with pytest.raises(RuntimeError, match="already reaped"):
        domain.close_confirmed(time.monotonic() + 1)
    assert signals == []
    with pytest.raises(RuntimeError, match="launch boundary"):
        _exec_process.ExecProcessDomain(root, None)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_group_signal_precedes_any_absence_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    events: list[str] = []
    original = os.killpg
    listing = process_group_closure.group_members

    def signal_group(pid: int, sig: int) -> None:
        events.append("signal")
        original(pid, sig)

    def sample(pgid: int) -> list[int]:
        events.append("sample")
        return listing(pgid)

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(process_group_closure, "group_members", sample)
    try:
        domain.close_confirmed(time.monotonic() + 5)
        assert events == ["signal", "sample"]
        assert root.wait(timeout=5) == -9
    finally:
        if root.returncode is None:
            original(root.pid, 9)
            root.wait(timeout=5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_signal_failure_retains_unreaped_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import errno

    from shared.native_process.ownership import OwnedProcess

    receipt = tmp_path / "child"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", code, str(receipt)]
    )
    child: OwnedProcess | None = None
    original = os.killpg

    def denied(_pid: int, _sig: int) -> None:
        raise PermissionError(errno.EPERM, "injected signal refusal")

    try:
        deadline = time.monotonic() + 5
        while not receipt.exists() or not _ended(psutil.Process(root.pid)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child = OwnedProcess.capture(psutil.Process(int(receipt.read_text())))
        monkeypatch.setattr(os, "killpg", denied)
        with pytest.raises((PermissionError, RuntimeError)) as caught:
            domain.close_confirmed(time.monotonic() + 1)
        assert root.returncode is None
        assert isinstance(caught.value, PermissionError) and "signal refusal" in str(caught.value)
        assert psutil.Process(root.pid).ppid() == os.getpid()
        assert child.live()
    finally:
        if child is not None:
            child.send_signal(9)
        if root.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                original(root.pid, 9)  # Exact fixture child already signalled above.
            root.wait(timeout=5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_native_capture_failure_retains_launched_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.exec_process_domain import ExecDomainBirthError
    from shared.native_process.ownership import OwnedProcess

    def denied(_process: psutil.Process) -> OwnedProcess:
        raise psutil.AccessDenied(_process.pid)

    monkeypatch.setattr(OwnedProcess, "capture", denied)
    with pytest.raises(ExecDomainBirthError) as caught:
        _exec_process.ExecProcessDomain.launch_posix(
            [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
        )
    root = caught.value.proc
    try:
        assert isinstance(caught.value.__cause__, psutil.AccessDenied)
        assert root.returncode is None
        assert psutil.Process(root.pid).ppid() == os.getpid()
    finally:
        os.killpg(root.pid, 9)
        root.wait(timeout=5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_signal_holds_native_pin_against_concurrent_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    entered, release, reaped = threading.Event(), threading.Event(), threading.Event()
    original = os.killpg

    def signal_group(pid: int, sig: int) -> None:
        entered.set()
        assert release.wait(5)
        assert root.returncode is None
        original(pid, sig)

    def wait() -> None:
        root.wait(timeout=5)
        reaped.set()

    monkeypatch.setattr(os, "killpg", signal_group)
    signaler = threading.Thread(target=domain.signal, args=(9,))
    waiter = threading.Thread(target=wait)
    try:
        signaler.start()
        assert entered.wait(5)
        waiter.start()
        assert not reaped.wait(0.05)
        release.set()
        signaler.join(5)
        waiter.join(5)
        assert reaped.is_set()
    finally:
        release.set()
        if root.returncode is None:
            original(root.pid, 9)
            root.wait(timeout=5)
        signaler.join(5)
        if waiter.ident is not None:
            waiter.join(5)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX retained child authority")
def test_confirmed_domain_does_not_reobserve_reused_numeric_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "pass"]
    )
    domain.close_confirmed(time.monotonic() + 5)
    root.wait(timeout=5)

    def unknown(_pid: int) -> bool:
        raise AssertionError("terminal domain cannot inspect a new numeric group")

    monkeypatch.setattr("shared.exec_process_domain._process_group_has_live_member", unknown)
    monkeypatch.setattr(process_group_closure, "group_members", unknown)
    domain.close_confirmed(time.monotonic() + 5)


@pytest.mark.skipif(sys.platform != "darwin", reason="XNU kernel group listing")
def test_group_listing_names_exited_leader_and_live_members(tmp_path: Path) -> None:
    receipt = tmp_path / "child"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", code, str(receipt)]
    )
    try:
        deadline = time.monotonic() + 5
        while not receipt.exists() or not _ended(psutil.Process(root.pid)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child = int(receipt.read_text())
        # The unreaped leader stays listed after exit, alongside its live member.
        assert process_group_closure.group_members(root.pid) == sorted([root.pid, child])
        domain.close_confirmed(time.monotonic() + 5)
        assert process_group_closure.group_members(root.pid) == [root.pid]
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(root.pid, 9)
        root.wait(timeout=5)


def _late_listing_domain(
    monkeypatch: pytest.MonkeyPatch, late_rounds: int | None
) -> tuple[subprocess.Popen[bytes], _exec_process.ExecProcessDomain, list[str]]:
    """An empty live sample whose kernel listing names a late member for some rounds.

    `late_rounds=None` keeps listing the late member in every round.
    """
    root, domain = _exec_process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    events: list[str] = []
    original = os.killpg

    def signal_group(pid: int, sig: int) -> None:
        events.append("signal")
        original(pid, sig)

    def empty(_pid: int) -> bool:
        return False

    def listing(pgid: int) -> list[int]:
        assert pgid == root.pid
        events.append("listing")
        rounds = events.count("listing")
        # A member forked after the sample enumerated PIDs; never signalled by number.
        late = late_rounds is None or rounds <= late_rounds
        return [root.pid, 0x7FFFFFFF] if late else [root.pid]

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr("shared.exec_process_domain._process_group_has_live_member", empty)
    monkeypatch.setattr(process_group_closure, "group_members", listing)
    return root, domain, events


@pytest.mark.skipif(sys.platform != "darwin", reason="XNU late-fork closure listing")
def test_listed_late_member_forces_another_signal_round(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain, events = _late_listing_domain(monkeypatch, late_rounds=1)
    try:
        domain.close_confirmed(time.monotonic() + 5)
        # The second round signals before its listing; XNU answers the
        # zombie-only group with EPERM, which the round then verifies.
        assert events == ["signal", "listing", "signal", "listing"]
        assert root.wait(timeout=5) == -9
    finally:
        if root.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(root.pid, 9)
            root.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "darwin", reason="XNU late-fork closure listing")
def test_persistent_late_member_leaves_leader_unreaped(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain, events = _late_listing_domain(monkeypatch, late_rounds=None)
    try:
        with pytest.raises(TimeoutError, match="besides its leader"):
            domain.close_confirmed(time.monotonic() + 0.5)
        assert events.count("signal") >= 2
        assert root.returncode is None
        assert psutil.Process(root.pid).ppid() == os.getpid()
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(root.pid, 9)
        root.wait(timeout=5)
