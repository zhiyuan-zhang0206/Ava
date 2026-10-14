"""Root driver signal delivery: pid reuse, start-tick identity and reaping boundaries.

Split from `test_root_driver.py`, which holds the readiness and launch-input tests and the shared helpers.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.sessions.tests.refuse_helper import refuse_helper
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.tests.test_root_driver import (
    status,
)

# Keep the public readiness entry point real when this owner suite reaches it.
pytestmark = pytest.mark.real_service_readiness_gate


def _mock_pidfd_delivery(monkeypatch: pytest.MonkeyPatch, signals: list[str]) -> None:
    from base.native_process import ownership as proc_tree

    def open_pidfd(_pid: int) -> int:
        return os.open(os.devnull, os.O_RDONLY)

    def deliver(descriptor: int, signum: int) -> None:
        os.fstat(descriptor)  # The native handle must remain open during delivery.
        signals.append("term" if signum == signal.SIGTERM else "kill")

    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(proc_tree.pidfd, "open_process", open_pidfd)
    monkeypatch.setattr(proc_tree.pidfd, "send_signal", deliver)


def test_signals_reject_reuse_inside_legacy_birth_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import psutil

    from base.native_process.ownership import OwnedProcess

    old = OwnedProcess(12345, 10.0, 100)
    replacement = OwnedProcess(12345, 10.01, 101)
    signals: list[str] = []
    _mock_pidfd_delivery(monkeypatch, signals)

    class NativeProcess:
        def terminate(self) -> None:
            signals.append("term")

        def kill(self) -> None:
            signals.append("kill")

        def send_signal(self, signum: int) -> None:
            signals.append("term" if signum == signal.SIGTERM else "kill")

    def process(_pid: int) -> NativeProcess:
        return NativeProcess()

    def live(_self: OwnedProcess) -> bool:
        # The old generation was alive at the pre-observation. PID reuse happens
        # before the signal's psutil handle is captured.
        return True

    def capture(_process: object) -> OwnedProcess:
        return replacement

    def legacy_match(_self: OwnedProcess, _process: object) -> bool:
        return True  # Both births fit the retired +/- 2 second compatibility window.

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(capture))
    monkeypatch.setattr(OwnedProcess, "birth_matches", legacy_match)
    monkeypatch.setattr(driver, "helper_spawn_committed", lambda: False)
    with pytest.raises(RuntimeError, match="identity changed"):
        snapshot = {"root": {"pid": old.pid, "create_time": old.birth, "starttime": old.starttime}}
        driver.stop_root_process(tmp_path, object(), snapshot, timeout_s=0, helper=refuse_helper)
    assert signals == []


def test_root_signals_use_start_ticks_when_wall_birth_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import psutil

    from base.native_process.ownership import OwnedProcess

    captured = OwnedProcess(12345, 10.0, 100)
    observed = OwnedProcess(12345, 3610.0, 100)
    signals: list[str] = []
    _mock_pidfd_delivery(monkeypatch, signals)

    class NativeProcess:
        def terminate(self) -> None:
            signals.append("term")

        def kill(self) -> None:
            signals.append("kill")

        def send_signal(self, signum: int) -> None:
            signals.append("term" if signum == signal.SIGTERM else "kill")

    def process(_pid: int) -> NativeProcess:
        return NativeProcess()

    def capture(_process: object) -> OwnedProcess:
        return observed

    def live(_self: OwnedProcess) -> bool:
        return not signals

    def ignore(_value: object) -> None:
        return None

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(capture))
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(driver, "helper_spawn_committed", lambda: False)
    monkeypatch.setattr(driver, "root_status", ignore)
    snapshot = {"root": {"pid": captured.pid, "create_time": captured.birth, "starttime": 100}}
    driver.stop_root_process(tmp_path, object(), snapshot, timeout_s=1, helper=refuse_helper)
    assert signals == ["term"]


@pytest.mark.parametrize("readiness_fails", [False, True])
def test_direct_child_cannot_be_reaped_by_an_unrelated_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readiness_fails: bool
) -> None:
    import gc
    import sys
    import time

    import psutil

    def command(_run: Path, _manifest: Path, _runtime: object = None) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def await_status(_client: object, _run_dir: Path, **_kwargs: object) -> dict[str, Any]:
        if readiness_fails:
            raise driver.RootDriverError("readiness failed")
        return status()

    monkeypatch.setattr(driver, "root_argv", command)
    monkeypatch.setattr(driver, "helper_spawn_committed", lambda: False)
    monkeypatch.setattr(driver, "await_root_status", await_status)
    children: list[subprocess.Popen[bytes]] = []
    try:
        driver.bring_up_root(
            tmp_path,
            tmp_path,
            tmp_path / "manifest",
            object(),
            {},
            retained_children=children,
            helper=refuse_helper,
        )
    except driver.RootDriverError:
        assert readiness_fails
    else:
        assert not readiness_fails
    assert len(children) == 1
    pid = children[0].pid
    gc.collect()
    deadline = time.monotonic() + 5
    try:
        while psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "child did not exit"
            time.sleep(0.01)
        # Popen cleans its abandoned-child table here. Our retained child must
        # stay unreaped while the caller retains it after the launch returns.
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=5)
        assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        children[0].wait(timeout=5)
