"""Captured process comparisons use Linux ticks, never drifting boot wall time."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from shared.native_process import native_boot_id, pidfd
from shared.native_process import ownership as proc_tree
from shared.native_process.ownership import OwnedProcess


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 101.0, 80), True),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 100.0, 81), False),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(42, 100.0, 80), False),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 100.0, None), False),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.0, 80), False),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.0, None), True),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.01, None), False),
    ],
)
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_same_birth(
    monkeypatch: pytest.MonkeyPatch,
    left: OwnedProcess,
    right: OwnedProcess,
    same: bool,
    platform: str,
) -> None:
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform=platform))
    if platform == "linux" and (left.starttime is None or right.starttime is None):
        same = False
    assert left.same_birth(right) is same
    assert right.same_birth(left) is same


def test_missing_linux_ticks_never_authorize_live_or_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))

    def unavailable(_pid: int) -> None:
        return None

    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", unavailable)
    identity = OwnedProcess(os.getpid(), 1.0, None)
    for observe in (identity.live, identity.birth_key):
        with pytest.raises(RuntimeError, match="Linux start ticks"):
            observe()
    with pytest.raises(RuntimeError, match="Linux start ticks"):
        OwnedProcess.capture(psutil.Process())


def test_lost_second_tick_read_is_unknown_for_existing_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([123, None])

    def read_ticks(_pid: int) -> int | None:
        return next(readings)

    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", read_ticks)
    with pytest.raises(RuntimeError, match="cannot finish Linux start ticks capture"):
        OwnedProcess.capture(psutil.Process())


@pytest.mark.parametrize("ticks", [0, -1, True])
def test_invalid_current_ticks_never_prove_native_exit(
    monkeypatch: pytest.MonkeyPatch, ticks: int
) -> None:
    def read_ticks(_pid: int) -> int:
        return ticks

    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", read_ticks)
    identity = OwnedProcess(os.getpid(), 1.0, 123)
    for observe in (identity.live, lambda: OwnedProcess.capture(psutil.Process())):
        with pytest.raises(RuntimeError, match="invalid Linux start ticks"):
            observe()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal custody")
def test_native_signal_keeps_exact_birth_and_preserves_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [
        subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"])
        for _ in range(2)
    ]
    try:
        owner = OwnedProcess.capture(psutil.Process(children[0].pid))
        sibling = OwnedProcess.capture(psutil.Process(children[1].pid))
        if sys.platform == "linux":
            # The pinned managed interpreter omits these optional Python APIs.
            monkeypatch.delattr(os, "pidfd_open", raising=False)
            monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
            original = proc_tree.stable_create_time
            monkeypatch.setattr(
                proc_tree, "stable_create_time", lambda process: original(process) + 3600
            )
        assert owner.send_signal(signal.SIGTERM)
        assert children[0].wait(timeout=5) == -signal.SIGTERM
        assert sibling.live()
        assert not owner.send_signal(signal.SIGTERM)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("operation", ["open", "signal"])
@pytest.mark.parametrize(
    ("code", "error"),
    [(errno.ESRCH, ProcessLookupError), (errno.EPERM, PermissionError), (errno.ENOSYS, OSError)],
)
def test_native_pidfd_errno_remains_an_authority_failure(
    monkeypatch: pytest.MonkeyPatch, operation: str, code: int, error: type[OSError]
) -> None:
    def refused(*_args: object) -> int:
        ctypes.set_errno(code)
        return -1

    api = SimpleNamespace(pidfd_open=refused, pidfd_send_signal=refused)
    monkeypatch.setattr(pidfd, "_api", lambda: api)
    with pytest.raises(error) as failure:
        if operation == "open":
            pidfd.open_process(41)
        else:
            pidfd.send_signal(70, signal.SIGTERM)
    assert failure.value.errno == code


def test_missing_libc_pidfd_support_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def library(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    monkeypatch.setattr(pidfd, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(ctypes, "CDLL", library)
    with pytest.raises(RuntimeError, match="requires libc pidfd support"):
        pidfd._api.__wrapped__()


def test_pidfd_preflight_closes_descriptor_when_signal_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)

    def refused(_fd: int, _signum: int) -> None:
        raise PermissionError(errno.EPERM, "native signal denied")

    def opened(_pid: int) -> int:
        return descriptor

    monkeypatch.setattr(pidfd, "open_process", opened)
    monkeypatch.setattr(pidfd, "send_signal", refused)
    with pytest.raises(PermissionError):
        pidfd.require_available()
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


@pytest.mark.skipif(sys.platform != "linux", reason="actual libc pidfd custody")
def test_native_descriptor_retains_exited_task_without_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"])
    descriptor = pidfd.open_process(child.pid)
    try:
        assert not os.get_inheritable(descriptor)
        pidfd.require_available()
        pidfd.send_signal(descriptor, signal.SIGTERM)
        assert child.wait(timeout=5) == -signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            pidfd.send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_native_boot_scope_is_explicit_and_stable() -> None:
    first = native_boot_id()
    assert first == native_boot_id()
    assert (first is None) == (sys.platform == "win32")


def test_recapture_retains_original_receipt_per_native_birth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))
    original = OwnedProcess(41, 100.0, 80)
    replacement = OwnedProcess(41, 100.0, 81)
    retained = {original}
    proc_tree.retain_processes(retained, [OwnedProcess(41, 3700.0, 80), replacement])
    assert retained == {original, replacement}
    assert OwnedProcess(41, 3700.0, 80) not in retained


@pytest.mark.parametrize("cached_birth", [None, 1.0])
def test_tree_capture_does_not_adopt_stale_child_listing(
    monkeypatch: pytest.MonkeyPatch, cached_birth: float | None
) -> None:
    """A stale PID map, with either an old or fresh Process, grants no custody."""
    children = [
        subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"])
        for _ in range(2)
    ]
    try:
        parent = OwnedProcess.capture(psutil.Process(children[0].pid))
        unrelated = psutil.Process(children[1].pid)
        if cached_birth is not None:
            monkeypatch.setattr(unrelated, "create_time", lambda: cached_birth)

        def stale_children(
            _process: psutil.Process, recursive: bool = False
        ) -> list[psutil.Process]:
            return [unrelated]

        monkeypatch.setattr(psutil.Process, "children", stale_children)
        captured = proc_tree.capture_tree(parent)
        assert captured == {parent}
        assert children[1].poll() is None
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)
