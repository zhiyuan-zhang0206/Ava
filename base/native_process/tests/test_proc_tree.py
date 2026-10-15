from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

import psutil
import pytest

from base.native_process import ownership as proc_tree
from base.native_process import pid_starttime_ticks
from base.native_process.os_platform import is_linux
from base.native_process.ownership import OwnedProcess, stable_create_time


def _identity_with_drift(offset: float) -> OwnedProcess:
    return OwnedProcess(os.getpid(), stable_create_time(psutil.Process()) + offset, None)


@pytest.mark.parametrize("offset", [-1.0, 1.0, -0.0001, 0.0001])
def test_live_rejects_a_different_native_birth(offset: float) -> None:
    if is_linux():
        with pytest.raises(RuntimeError, match="missing Linux start ticks"):
            _identity_with_drift(offset).live()
    else:
        assert not _identity_with_drift(offset).live()


def test_live_accepts_the_exact_captured_native_birth() -> None:
    assert OwnedProcess.capture(psutil.Process()).live()


def test_birth_matches_exposes_the_same_rule() -> None:
    process = psutil.Process()
    native = OwnedProcess.capture(process)
    assert native.birth_matches(process)
    changed = OwnedProcess(
        native.pid,
        native.birth + 0.0001,
        native.starttime + 1 if native.starttime is not None else None,
    )
    assert not changed.birth_matches(process)


@pytest.mark.skipif(not is_linux(), reason="Linux /proc start-time identity")
def test_live_converges_when_the_proc_entry_vanishes_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = child.pid
    starttime = pid_starttime_ticks(pid)
    assert starttime is not None
    real_read = pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", reaping_read)
    try:
        assert OwnedProcess(pid, 0.5, starttime).live() is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(not is_linux(), reason="Linux /proc start-time identity")
def test_live_keeps_the_error_when_a_present_process_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = psutil.Process()
    identity = OwnedProcess(process.pid, process.create_time(), pid_starttime_ticks(process.pid))
    assert identity.starttime is not None

    def unreadable_read(_pid: int) -> int | None:
        return None

    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", unreadable_read)
    with pytest.raises(RuntimeError, match="cannot verify process identity"):
        identity.live()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)
@pytest.mark.parametrize("seconds", (60.0, 3600.0))
def test_stable_create_time_ignores_a_simulated_clock_step(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    from psutil import _psosx

    psosx: Any = _psosx

    key_before = stable_create_time(psutil.Process())
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", psosx.INIT_BOOT_TIME + seconds)
    public_moved = psutil.Process().create_time()
    assert stable_create_time(psutil.Process()) == key_before
    assert abs(public_moved - key_before) > 2.0


@pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)
@pytest.mark.parametrize("seconds", (60.0, 3600.0))
def test_capture_and_verify_span_clock_epochs(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    from psutil import _psosx

    psosx: Any = _psosx

    base = psosx.INIT_BOOT_TIME
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + seconds)
    identity = OwnedProcess.capture(psutil.Process())
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
    assert identity.live()


class _FakeProcess:
    def __init__(self, pid: int, name: str, argv: list[str], parent: _FakeProcess | None) -> None:
        self.pid = pid
        self._name = name
        self._argv = argv
        self._parent = parent

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return f"/usr/bin/{self._name}"

    def ppid(self) -> int:
        return self._parent.pid if self._parent is not None else 0

    def parent(self) -> _FakeProcess | None:
        return self._parent

    def cmdline(self) -> list[str]:
        return self._argv


def test_process_metadata_records_the_script_of_a_node_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _FakeProcess(10, "zsh", ["-zsh"], None)
    node = _FakeProcess(20, "node", ["node", "/opt/homebrew/bin/dsh", "web"], shell)
    caller = _FakeProcess(30, "python3.12", ["python", "-m", "cli"], node)
    monkeypatch.setattr(proc_tree.psutil, "Process", lambda: caller)

    def capture(process: _FakeProcess) -> OwnedProcess:
        return OwnedProcess(process.pid, 1.0, None)

    monkeypatch.setattr(proc_tree.OwnedProcess, "capture", capture)
    metadata = proc_tree.process_metadata()
    assert metadata["pid"] == 30 and "script" not in metadata
    node_facts, shell_facts = metadata["ancestors"]
    assert node_facts["name"] == "node" and node_facts["script"] == "/opt/homebrew/bin/dsh"
    assert "script" not in shell_facts
