from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

import psutil

from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig

_CHILD = Path(__file__).with_name("child_unit.py")


def _supervisor(run_dir: Path, *scenario: str) -> Supervisor:
    unit = UnitManifest(
        "svc", (sys.executable, str(_CHILD), *scenario), RestartPolicy.NEVER, "root"
    )
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


async def test_late_child_is_not_a_service_replacement_gate(tmp_path: Path) -> None:
    ready, child_file = tmp_path / "ready", tmp_path / "late-child.pid"
    owner = _supervisor(tmp_path / "root", "forked_late_child", str(child_file), str(ready))
    await owner.start()
    await _wait_for(ready)
    child: psutil.Process | None = None
    try:
        units = cast("list[dict[str, Any]]", (await owner.status())["units"])
        leader = cast(int, units[0]["pid"])
        assert os.getpgid(leader) == leader
        assert os.getsid(leader) == os.getsid(0)
        await owner.down("svc")
        await _wait_for(child_file)
        child = _late_child(child_file)
        assert child is not None and not _gone(child)
        assert owner._units["svc"].generation is None
        assert not (tmp_path / "root" / "custody").exists()
    finally:
        if child is not None and not _gone(child):
            child.kill()
        await owner.shutdown()
