from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import monotonic
from typing import Any, cast

import psutil
import pytest

from base.native_process.root_control.client import RootClient
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.server import ControlServer
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig

_DEFAULT_WINDOW_S = 0.2


_CHILD = Path(__file__).with_name("child_unit.py")


def root(
    run_dir: Path,
    *scenario: str,
    stop_timeout_s: float | None = None,
    default_window_s: float = _DEFAULT_WINDOW_S,
) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=stop_timeout_s,
    )
    return Supervisor(
        UnitRegistry([unit]),
        run_dir=run_dir,
        config=SupervisorConfig(stop_timeout_s=default_window_s),
    )


async def state(owner: Supervisor) -> str:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]["state"]


async def wait_file(path: Path) -> None:
    for _ in range(250):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"child did not write {path}")


def closes_slowly_after_term(ready: Path, close_s: float) -> tuple[str, ...]:
    return ("closes_slowly", str(ready), str(close_s))


async def test_unit_closing_past_the_default_window_stops_inside_its_declared_window(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, *closes_slowly_after_term(ready, 0.8), stop_timeout_s=3.0)
    await owner.start()
    await wait_file(ready)
    result = await owner.down("worker")
    assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
    assert await state(owner) == "stopped"
    assert not (tmp_path / "custody").exists()
    await owner.shutdown()


async def test_unit_declaring_no_window_keeps_the_default_and_the_refusal_names_it(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, *closes_slowly_after_term(ready, 0.8))
    await owner.start()
    await wait_file(ready)
    try:
        with pytest.raises(RuntimeError, match=r"did not stop within its 0\.2s window"):
            await owner.down("worker")
        assert not (tmp_path / "custody").exists()
        for _ in range(100):
            if await state(owner) == "stopped":
                break
            await asyncio.sleep(0.02)
        await owner.down("worker")
        assert not (tmp_path / "custody").exists()
    finally:
        await owner.shutdown()


async def test_force_escalates_after_the_units_declared_window(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, "ignore_term", str(ready), stop_timeout_s=0.8)
    await owner.start()
    await wait_file(ready)
    pid = cast("list[dict[str, Any]]", (await owner.status())["units"])[0]["pid"]
    began = monotonic()
    await owner.down("worker", force=True)
    assert monotonic() - began >= 0.8, "force killed before the unit's declared window ended"
    assert not psutil.pid_exists(pid)
    await owner.shutdown()


async def test_down_over_the_control_socket_succeeds_for_a_unit_closing_past_ten_seconds(
    short_tmp: Path,
) -> None:
    ready = short_tmp / "ready"
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *closes_slowly_after_term(ready, 12.5)),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=20.0,
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=short_tmp)
    server = ControlServer(short_tmp / "meiva-root.sock", owner.dispatch)
    await server.start()
    try:
        await owner.start()
        await wait_file(ready)
        response = await asyncio.to_thread(
            RootClient(short_tmp / "meiva-root.sock", timeout=60).down, "worker"
        )
        assert response["ok"] is True, response
        assert await state(owner) == "stopped"
        assert not (short_tmp / "custody").exists()
    finally:
        await owner.shutdown()
        await server.close()
