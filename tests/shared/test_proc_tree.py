"""Birth identity: the stable key is exact; legacy create_time reads carry the tolerance."""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys

import psutil
import pytest

from shared import proc_tree
from shared.platform import IS_LINUX
from shared.proc_tree import OwnedProcess, create_time_matches, stable_create_time
from shared.session_record import pid_starttime_ticks


def _identity_with_drift(offset: float) -> OwnedProcess:
    # starttime=None forces the create_time fallback used on macOS and by
    # legacy records (Linux CI exercises the same branch).
    return OwnedProcess(os.getpid(), psutil.Process().create_time() + offset, None)


def test_live_tolerates_whole_second_create_time_drift() -> None:
    """One live process can move by whole seconds; either direction is the same process.

    macOS psutil re-derives create_time from the wall clock and applies a
    boot-time correction quantized to whole seconds. On 2026-09-12 the stop path
    refused a live host over exactly 1.000000s of drift.
    """
    assert _identity_with_drift(-1.0).live()
    assert _identity_with_drift(1.0).live()


def test_live_rejects_a_birth_beyond_the_tolerance() -> None:
    """A create_time outside the tolerance is still a changed process."""
    assert not _identity_with_drift(-3.0).live()
    assert not _identity_with_drift(60.0).live()


def test_birth_matches_exposes_the_same_rule() -> None:
    """Signal delivery and the deadline report call this guard directly."""
    process = psutil.Process()
    assert OwnedProcess(process.pid, process.create_time() + 1.0, None).birth_matches(process)
    assert not OwnedProcess(process.pid, process.create_time() + 60.0, None).birth_matches(process)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_live_converges_when_the_proc_entry_vanishes_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil validated the pid, then the raw read found no /proc entry: the
    tracked process was reaped in between, and that IS the exit.

    2026-09-20 wave-2: this window raised `cannot verify process identity` out
    of the stop wait and aborted the whole cluster update with the tracked
    daemon already exiting. The reap is timed by the patched read so the
    interleave is deterministic; the read itself, psutil and pid_exists stay
    real.
    """
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


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_live_keeps_the_error_when_a_present_process_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid that still exists while its start time cannot be read is not
    converged: the identity question genuinely went unanswered, and the loud
    error is the safety answer."""
    process = psutil.Process()
    identity = OwnedProcess(process.pid, process.create_time(), pid_starttime_ticks(process.pid))
    assert identity.starttime is not None

    def unreadable_read(_pid: int) -> int | None:
        return None

    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", unreadable_read)
    with pytest.raises(RuntimeError, match="cannot verify process identity"):
        identity.live()


def test_create_time_matches_accepts_whole_second_moves() -> None:
    """One live process can move by whole seconds in either direction.

    The 2.0s span itself is still the same process; .5 fractions are exact in
    binary, so the boundary assertion does not depend on float rounding.
    """
    assert create_time_matches(99.5, 98.5)
    assert create_time_matches(98.5, 99.5)
    assert create_time_matches(100.5, 98.5)


def test_create_time_matches_rejects_readings_beyond_the_tolerance() -> None:
    """A reading outside the tolerance is positive evidence of a different process."""
    assert not create_time_matches(101.5, 98.5)
    assert not create_time_matches(158.5, 98.5)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)
@pytest.mark.parametrize("seconds", (60.0, 3600.0))
def test_stable_create_time_ignores_a_simulated_clock_step(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """The identity key must not move when the wall clock (boot epoch) steps.

    psutil's public create_time() adds `INIT_BOOT_TIME - boot_time()`, so the
    simulated step moves the public reading — the artifact a 2s tolerance
    cannot absorb — while the key stays identical.
    """
    psosx = importlib.import_module("psutil._psosx")
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
    """A record captured under one import epoch still verifies under another.

    One side stays unpatched on purpose: psutil's correction adds |diff| in
    either direction, so a -N patch models the same epoch as +N.
    """
    psosx = importlib.import_module("psutil._psosx")
    base = psosx.INIT_BOOT_TIME
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + seconds)
    identity = OwnedProcess.capture(psutil.Process())
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
    assert identity.live()
