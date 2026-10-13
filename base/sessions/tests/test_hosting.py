"""`hosting_supervised_session`: the supervised session this process runs inside."""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest

from base.native_process import pid_starttime_ticks
from base.native_process.os_platform import is_linux
from base.paths import run_dir
from base.sessions.hosting import hosting_supervised_session
from base.sessions.record import SessionRecord


def test_hosting_supervised_session_resolves_home_when_called(unit_home: Path) -> None:
    """The settings-free proc module loads its home reader only at this boundary."""
    name = "ava-test-hosting-current"
    path = run_dir() / "sessions" / f"{name}.json"
    SessionRecord(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=pid_starttime_ticks(os.getpid()),
    ).write(path)
    try:
        assert hosting_supervised_session() == name
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(not is_linux(), reason="Linux /proc start-time identity")
def test_hosting_supervised_session_uses_starttime_despite_wall_clock_drift(
    unit_home: Path,
) -> None:
    """Update lineage recognizes a matching stable tick even when btime drifted."""
    starttime = pid_starttime_ticks(os.getpid())
    assert starttime is not None
    name = "ava-test-hosting-starttime"
    path = run_dir() / "sessions" / f"{name}.json"
    SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=starttime,
    ).write(path)
    try:
        assert hosting_supervised_session() == name
    finally:
        path.unlink(missing_ok=True)
