"""A zombie in a former service group does not gate direct-child completion.

The disposable holder delays its own child's reap deliberately. Root stops its
known direct child without enumerating the group or waiting for another parent
that it does not own. The holder and its child are explicitly cleaned up here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import psutil

from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig


def _supervisor(run_dir: Path) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        RestartPolicy.ALWAYS,
        "root",
    )
    return Supervisor(
        UnitRegistry([unit]), run_dir=run_dir, config=SupervisorConfig(stop_timeout_s=0.2)
    )


def _holder(pgid: int, reap: Path) -> subprocess.Popen[str]:
    """Create a separate parent that joins only its child to the service group."""
    return subprocess.Popen(  # noqa: S603 - disposable interpreter and private test paths
        [
            sys.executable,
            "-c",
            "import pathlib,subprocess,sys,time\n"
            "member=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            " process_group=int(sys.argv[1]))\n"
            "print(member.pid,flush=True)\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
            "member.wait()\n",
            str(pgid),
            str(reap),
        ],
        stdout=subprocess.PIPE,
        text=True,
        process_group=0,
    )


async def _wait_zombie(member: psutil.Process) -> None:
    for _ in range(100):
        if member.status() == psutil.STATUS_ZOMBIE:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the holder has not retained an unreaped child")


async def _cleanup_holder(
    holder: subprocess.Popen[str], member: psutil.Process | None, reap: Path
) -> None:
    reap.touch()
    if member is not None:
        with contextlib.suppress(psutil.NoSuchProcess):
            if member.status() != psutil.STATUS_ZOMBIE:
                member.kill()
    try:
        await asyncio.to_thread(holder.wait, timeout=5)
    except subprocess.TimeoutExpired:
        holder.kill()
        await asyncio.to_thread(holder.wait, timeout=5)
    if holder.stdout is not None:
        holder.stdout.close()


async def test_a_zombie_group_member_does_not_block_the_direct_child_stop(tmp_path: Path) -> None:
    reap = tmp_path / "reap"
    owner = _supervisor(tmp_path)
    await owner.start()
    generation = owner._units["worker"].generation
    assert generation is not None
    pgid = generation.proc.pid
    holder = _holder(pgid, reap)
    member: psutil.Process | None = None
    try:
        assert holder.stdout is not None
        member = psutil.Process(int(holder.stdout.readline()))
        assert os.getpgid(member.pid) == pgid
        member.send_signal(signal.SIGKILL)
        await _wait_zombie(member)

        result = await owner.down("worker")

        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
        assert generation.exited.is_set() and generation.proc.returncode is not None
        assert owner._units["worker"].generation is None
        assert member.status() == psutil.STATUS_ZOMBIE
        assert member.is_running(), "the other parent still owns an unreaped child"
        assert not (tmp_path / "custody").exists()
        reap.touch()
        assert await asyncio.to_thread(holder.wait, timeout=5) == 0
        assert not psutil.pid_exists(member.pid), "only its actual parent reaps the zombie"
    finally:
        try:
            await _cleanup_holder(holder, member, reap)
        finally:
            await owner.shutdown()
