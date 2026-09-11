"""Birth identity: starttime ticks are exact; create_time reads carry the tolerance."""

from __future__ import annotations

import os

import psutil

from shared.proc_tree import OwnedProcess, create_time_matches


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
