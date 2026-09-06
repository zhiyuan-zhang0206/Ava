"""A normal stop cannot certify an empty tree after its leader reparents a child."""

from pathlib import Path

import psutil
import pytest

from cli.commands import _maintenance_stop as stop
from shared.session_backend import PosixProcSessionBackend
from shared.session_record import SessionRecord
from tests.cli.test_maintenance_stop import Launcher
from tests.cli.test_maintenance_stop import home as home
from tests.cli.test_maintenance_stop import launch as launch


def test_same_group_child_created_during_signal_prevents_success(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_file = home / "late-child.pid"
    parent = launch(
        "late-child",
        f"""
import subprocess, signal, sys, time, pathlib, os

def finish(*_):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path({str(child_file)!r}).write_text(str(child.pid))
    os._exit(0)
signal.signal(signal.SIGTERM, finish)
print('ready', flush=True)
time.sleep(60)
""",
    )
    original = PosixProcSessionBackend.graceful_signal

    def deliver(
        backend: PosixProcSessionBackend,
        name: str,
        *,
        expected: SessionRecord | None = None,
    ) -> bool:
        result = original(backend, name, expected=expected)
        parent.wait(timeout=2)  # Make the actual leader exit before the next snapshot.
        return result

    monkeypatch.setattr(PosixProcSessionBackend, "graceful_signal", deliver)
    try:
        with pytest.raises(TimeoutError, match="occupied process groups"):
            stop.stop_services(0.3)
        child = psutil.Process(int(child_file.read_text()))
        assert child.is_running()  # The refusal must not become a force kill.
    finally:
        if child_file.exists():
            try:
                child = psutil.Process(int(child_file.read_text()))
                child.kill()  # Exact private fixture cleanup after the assertions only.
            except psutil.NoSuchProcess:
                pass
