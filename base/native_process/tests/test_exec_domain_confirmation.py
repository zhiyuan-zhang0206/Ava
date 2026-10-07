"""Positive managed-domain closure, separate from signal submission or root exit."""

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from base.native_process import exec_domain as _process
from base.native_process import group_closure


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
    root, domain = _process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", code, str(gate), str(receipt)],
        new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    member: psutil.Process | None = None
    try:
        gate.write_text("attached")
        until = time.monotonic() + 10
        while not receipt.exists() or not _ended(psutil.Process(root.pid)):
            if time.monotonic() > until:
                raise AssertionError("fixture root did not exit without reap")
            time.sleep(0.01)
        member = psutil.Process(int(receipt.read_text()))
        assert not _ended(member)
        close_deadline = time.monotonic() + 5
        domain.close_confirmed(close_deadline)
        while time.monotonic() < close_deadline and not _ended(member):
            time.sleep(0.01)
        assert _ended(member)
        assert root.wait(timeout=5) == 0
    finally:
        if root.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(root.pid, 9)
            root.wait(timeout=5)
        if member is not None and not _ended(member):
            member.kill()
            member.wait(timeout=5)


def test_live_group_after_signal_is_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    process, domain = _process.ExecProcessDomain.launch_posix(
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


def test_fast_exit_owner_closes_before_reap() -> None:
    for _ in range(8):
        root, domain = _process.ExecProcessDomain.launch_posix([sys.executable, "-I", "-c", "pass"])
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


def test_reaped_owner_cannot_signal_a_reused_group(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain = _process.ExecProcessDomain.launch_posix([sys.executable, "-I", "-c", "pass"])
    root.wait(timeout=5)
    signals: list[int] = []

    def recorded(pid: int, _signum: int) -> None:
        signals.append(pid)

    monkeypatch.setattr(os, "killpg", recorded)
    with pytest.raises(RuntimeError, match="already reaped"):
        domain.close_confirmed(time.monotonic() + 1)
    assert signals == []
    with pytest.raises(RuntimeError, match="launch boundary"):
        _process.ExecProcessDomain(root)


def test_group_signal_precedes_any_absence_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    root, domain = _process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    )
    events: list[str] = []
    original = os.killpg
    listing = group_closure.group_members

    def signal_group(pid: int, sig: int) -> None:
        events.append("signal")
        original(pid, sig)

    def sample(pgid: int) -> list[int]:
        events.append("sample")
        return listing(pgid)

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(group_closure, "group_members", sample)
    try:
        domain.close_confirmed(time.monotonic() + 5)
        assert events == ["signal", "sample"]
        assert root.wait(timeout=5) == -9
    finally:
        if root.returncode is None:
            original(root.pid, 9)
            root.wait(timeout=5)


def test_signal_failure_retains_unreaped_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import errno

    from base.native_process.ownership import OwnedProcess

    receipt = tmp_path / "child"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    root, domain = _process.ExecProcessDomain.launch_posix(
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


def test_native_capture_failure_retains_launched_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.native_process.exec_domain import ExecDomainBirthError
    from base.native_process.ownership import OwnedProcess

    def denied(_process: psutil.Process) -> OwnedProcess:
        raise psutil.AccessDenied(_process.pid)

    monkeypatch.setattr(OwnedProcess, "capture", denied)
    with pytest.raises(ExecDomainBirthError) as caught:
        _process.ExecProcessDomain.launch_posix(
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


def test_signal_holds_native_pin_against_concurrent_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    root, domain = _process.ExecProcessDomain.launch_posix(
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


def test_confirmed_domain_does_not_reobserve_reused_numeric_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, domain = _process.ExecProcessDomain.launch_posix([sys.executable, "-I", "-c", "pass"])
    domain.close_confirmed(time.monotonic() + 5)
    root.wait(timeout=5)

    def unknown(_pid: int) -> bool:
        raise AssertionError("terminal domain cannot inspect a new numeric group")

    monkeypatch.setattr("base.native_process.exec_domain._process_group_has_live_member", unknown)
    monkeypatch.setattr(group_closure, "group_members", unknown)
    domain.close_confirmed(time.monotonic() + 5)


@pytest.mark.skipif(sys.platform != "darwin", reason="XNU kernel group listing")
def test_group_listing_names_exited_leader_and_live_members(tmp_path: Path) -> None:
    receipt = tmp_path / "child"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    root, domain = _process.ExecProcessDomain.launch_posix(
        [sys.executable, "-I", "-c", code, str(receipt)]
    )
    try:
        deadline = time.monotonic() + 5
        while not receipt.exists() or not _ended(psutil.Process(root.pid)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child = int(receipt.read_text())
        # The unreaped leader stays listed after exit, alongside its live member.
        assert group_closure.group_members(root.pid) == sorted([root.pid, child])
        domain.close_confirmed(time.monotonic() + 5)
        assert group_closure.group_members(root.pid) == [root.pid]
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(root.pid, 9)
        root.wait(timeout=5)


def _late_listing_domain(
    monkeypatch: pytest.MonkeyPatch, late_rounds: int | None
) -> tuple[subprocess.Popen[bytes], _process.ExecProcessDomain, list[str]]:
    """An empty live sample whose kernel listing names a late member for some rounds.

    `late_rounds=None` keeps listing the late member in every round.
    """
    root, domain = _process.ExecProcessDomain.launch_posix(
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
    monkeypatch.setattr("base.native_process.exec_domain._process_group_has_live_member", empty)
    monkeypatch.setattr(group_closure, "group_members", listing)
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
