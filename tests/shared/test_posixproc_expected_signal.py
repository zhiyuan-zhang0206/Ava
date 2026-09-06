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


@pytest.mark.parametrize(
    ("stored_tick", "observed_ticks", "birth_delta", "should_signal"),
    [
        (123, (123, 123), -1.0, True),
        (123, (124,), 0.0, False),
        (123, (None,), 0.0, False),
        (123, (123, 124), 0.0, False),
        (None, (), -1.0, False),
        (None, (), 0.0, True),
    ],
    ids=[
        "same-ticks-epoch-drift",
        "different-ticks",
        "unreadable-ticks",
        "ticks-changed-before-signal",
        "legacy-birth-mismatch",
        "legacy-matching-birth",
    ],
)
def test_expected_identity_uses_stable_ticks_before_epoch_birth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_tick: int | None,
    observed_ticks: tuple[int | None, ...],
    birth_delta: float,
    should_signal: bool,
) -> None:
    """A real private child; only the WSL clock observation is simulated.

    WSL can change btime while /proc start ticks remain stable. Preserve the
    old recorded epoch to reproduce that discrepancy without changing the host
    clock. Never weaken unreadable/different ticks or the legacy birth guard.
    """
    import signal
    from dataclasses import replace

    from shared import session_record

    argv = [sys.executable, "-I", "-c", "import time;time.sleep(60)"]
    with subprocess.Popen(argv) as child:  # noqa: S603 — fixed isolated test child
        try:
            original = record_for(child.pid)
            expected = replace(
                original, create_time=original.create_time + birth_delta, starttime=stored_tick
            )
            path = tmp_path / "ava-private-ticks.json"
            expected.write(path)
            ticks = iter(observed_ticks)

            def read_ticks(_pid: int) -> int | None:
                return next(ticks)

            def record_path(_name: str) -> Path:
                return path

            monkeypatch.setattr(session_record, "pid_starttime_ticks", read_ticks)
            monkeypatch.setattr(posixproc, "_record_path", record_path)
            assert (
                posixproc.graceful_signal("ava-private-ticks", expected=expected) is should_signal
            )
            if should_signal:
                assert child.wait(timeout=3) == -signal.SIGTERM
            else:
                assert child.poll() is None
            assert SessionRecord.read(path) == expected
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
