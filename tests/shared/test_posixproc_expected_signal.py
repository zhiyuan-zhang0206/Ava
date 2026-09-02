"""Actual bystander safety when a named service record changes during stop."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from shared import posixproc
from shared.session_record import SessionRecord, pid_starttime_ticks

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX expected-record contract")


def record_for(pid: int) -> SessionRecord:
    birth = psutil.Process(pid).create_time()
    return SessionRecord(
        pid=pid,
        create_time=birth,
        cmd="isolated CI child",
        cwd="/",
        started_at=birth,
        starttime=pid_starttime_ticks(pid),
    )


def test_replaced_name_never_signals_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    with subprocess.Popen(argv) as old, subprocess.Popen(argv) as replacement:  # noqa: S603 — fixed isolated test children.
        try:
            expected, new = record_for(old.pid), record_for(replacement.pid)
            path = tmp_path / "ava-ops.json"
            expected.write(path)
            calls = 0

            def swapped_read(_name: str) -> SessionRecord:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return expected
                new.write(path)
                return new

            monkeypatch.setattr(posixproc, "_read_record", swapped_read)
            assert not posixproc.graceful_signal("ava-ops", expected=expected)
            assert calls == 2
            assert replacement.poll() is None
            assert old.poll() is None
            assert SessionRecord.read(path) == new
        finally:
            old.terminate()
            replacement.terminate()
            old.wait(timeout=10)
            replacement.wait(timeout=10)
