"""Exact native process identity remains independent from rollout controllers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from shared.native_process import pid_starttime_ticks
from shared.native_process.ownership import stable_create_time
from shared.platform import IS_LINUX
from shared.process_evidence import ExpectedProcess, observe_process


def test_exact_live_exited_and_reused_identity() -> None:
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        expected = ExpectedProcess(
            pid=child.pid,
            create_time=stable_create_time(psutil.Process(child.pid)),
            starttime=pid_starttime_ticks(child.pid),
        )
        assert observe_process(expected) == "alive"
        mismatch = expected.model_copy(
            update={
                "starttime": expected.starttime + 1 if expected.starttime is not None else None,
                "create_time": expected.create_time + 60,
            }
        )
        assert observe_process(mismatch) == "identity_mismatch"
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=5)
        assert observe_process(expected) == "exited"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_observe_process_reads_a_vanished_entry_as_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop-race window in the managed-writer observation: psutil validated
    the pid, then the raw read found no /proc entry because the process was
    reaped in between — that is the exit itself, not a lost observation."""
    from shared import process_evidence as observation

    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    pid = child.pid
    expected = ExpectedProcess(
        pid=pid,
        create_time=psutil.Process(pid).create_time(),
        starttime=pid_starttime_ticks(pid),
    )
    assert expected.starttime is not None
    real_read = pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(observation, "pid_starttime_ticks", reaping_read)
    try:
        assert observe_process(expected) == "exited"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_observe_process_keeps_unknown_when_a_present_process_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid that still exists while its start time cannot be read stays a
    full unknown — never collapsed into the exit it does not prove."""
    from shared import process_evidence as observation

    expected = ExpectedProcess(
        pid=os.getpid(), create_time=psutil.Process().create_time(), starttime=1
    )

    def unreadable_read(_pid: int) -> int | None:
        return None

    monkeypatch.setattr(observation, "pid_starttime_ticks", unreadable_read)
    assert observe_process(expected) == "unknown"


def test_whole_second_create_time_drift_refuses_the_process() -> None:
    """A whole-second birth mismatch cannot prove the identity of a live process."""
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        live = psutil.Process(child.pid).create_time()
        for offset in (-1.0, 1.0):
            expected = ExpectedProcess(pid=child.pid, create_time=live + offset, starttime=None)
            assert observe_process(expected) == ("unknown" if IS_LINUX else "identity_mismatch")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_runtime_consumers_do_not_load_retired_rollout_authority() -> None:
    repo = Path(__file__).resolve().parents[2]
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "import cli.release_build,shared.runtime_service_identity,shared.native_job_observation;"
        "loaded=[name for name in sys.modules if name.startswith("
        "('shared.managed_writer','shared.runtime_publication','cli.commands._update'))];"
        "assert not loaded, loaded"
    )
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed code and captured repo.
        [sys.executable, "-I", "-B", "-c", code, str(repo)], check=True
    )
