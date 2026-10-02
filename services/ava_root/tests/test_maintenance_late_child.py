"""A normal stop cannot certify an empty unit after its leader leaves a late child.

`ava stop` asks ava-root to stop its units. Each unit runs in its own process
group, and "stopped" is certified only by the kernel reporting that group empty
after the leader was reaped. A child forked while the leader handles SIGTERM is
still a member, so it is closed with the unit or keeps the stop from succeeding;
the graceful refusal never becomes a kill. A unit that calls setsid() leaves its
group by construction and is out of scope.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.supervisor import Supervisor, SupervisorConfig

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")

# The late child is created by the SIGTERM handler, right before the leader exits.
_POPEN_CHILD = """
import os, pathlib, signal, subprocess, sys, time
def finish(*_):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path({child_file!r}).write_text(str(child.pid))
    os._exit(0)
signal.signal(signal.SIGTERM, finish)
pathlib.Path({ready!r}).touch()
time.sleep(60)
"""

# A native fork (no exec) whose child ignores SIGTERM: only force may close it.
_FORKED_CHILD = """
import os, pathlib, signal, time
def finish(*_):
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
        os._exit(0)
    pathlib.Path({child_file!r}).write_text(str(pid))
    os._exit(0)
signal.signal(signal.SIGTERM, finish)
pathlib.Path({ready!r}).touch()
time.sleep(60)
"""


def _supervisor(run_dir: Path, code: str) -> Supervisor:
    unit = UnitManifest("svc", (sys.executable, "-c", code), RestartPolicy.NEVER, "root")
    return Supervisor(
        UnitRegistry([unit]), run_dir=run_dir, config=SupervisorConfig(stop_timeout_s=1.0)
    )


async def _wait_for(path: Path) -> None:
    for _ in range(250):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"unit did not write {path}")


def _late_child(child_file: Path) -> psutil.Process | None:
    """The late child, or None when it already exited and was reaped."""
    try:
        return psutil.Process(int(child_file.read_text()))
    except psutil.NoSuchProcess:
        return None


def _gone(process: psutil.Process | None) -> bool:
    if process is None:
        return True
    try:
        return process.status() == psutil.STATUS_ZOMBIE or not process.is_running()
    except psutil.NoSuchProcess:
        return True


async def test_late_child_created_during_signal_is_closed_with_its_unit(tmp_path: Path) -> None:
    ready, child_file = tmp_path / "ready", tmp_path / "late-child.pid"
    owner = _supervisor(
        tmp_path / "root", _POPEN_CHILD.format(child_file=str(child_file), ready=str(ready))
    )
    await owner.start()
    await _wait_for(ready)
    child: psutil.Process | None = None
    try:
        units = cast("list[dict[str, Any]]", (await owner.status())["units"])
        unit_pid = cast("int", units[0]["pid"])
        assert os.getpgid(unit_pid) == unit_pid  # its own group ...
        assert os.getsid(unit_pid) == os.getsid(0)  # ... in root's session
        await owner.down("svc")
        child = _late_child(child_file)
        assert _gone(child), "stop reported success while a group member survived"
        assert not list((tmp_path / "root" / "custody").iterdir())
    finally:
        if child is None and child_file.exists():
            child = _late_child(child_file)
        if child is not None and not _gone(child):
            child.kill()  # exact private fixture cleanup after a failed assertion
        await owner.shutdown()


async def test_late_child_ignoring_term_retains_ownership_until_force(tmp_path: Path) -> None:
    ready, child_file = tmp_path / "ready", tmp_path / "late-child.pid"
    owner = _supervisor(
        tmp_path / "root", _FORKED_CHILD.format(child_file=str(child_file), ready=str(ready))
    )
    await owner.start()
    await _wait_for(ready)
    child: psutil.Process | None = None
    try:
        with pytest.raises(RuntimeError, match="ownership retained") as refused:
            await owner.down("svc")
        child = _late_child(child_file)
        assert child is not None and not _gone(child)  # refusal never became a kill
        assert str(child.pid) in str(refused.value)
        assert (tmp_path / "root" / "custody" / "svc.json").exists()
        await owner.down("svc", force=True)
        assert _gone(child)
        assert not list((tmp_path / "root" / "custody").iterdir())
    finally:
        if child is not None and not _gone(child):
            child.kill()  # exact private fixture cleanup after a failed assertion
        await owner.shutdown()
