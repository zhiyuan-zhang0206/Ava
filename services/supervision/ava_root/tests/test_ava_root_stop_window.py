"""Root's TERM window is per unit, so a unit closing inside its own bound is stopped.

Two production units keep closing after SIGTERM for longer than root's default
window (10 s): the gateway drains connections up to its own budget, and the
browser-MCP daemon runs two bounded cleanup steps of 10 s each. Root used to
refuse both at its default window and report `internal: internal error` for a
stop that was going fine. A unit's manifest now declares its own window
(`stop_timeout_s`), which the roster derives from the unit's own bound; these
tests use real disposable processes and never signal anything but their own
children.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import monotonic
from typing import Any, cast

import psutil
import pytest

from base.native_process.root_control.client import RootClient
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.server import ControlServer
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig

_DEFAULT_WINDOW_S = 0.2  # scaled stand-in for root's 10 s default


def root(
    run_dir: Path,
    code: str,
    *,
    stop_timeout_s: float | None = None,
    default_window_s: float = _DEFAULT_WINDOW_S,
) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", "-c", code),
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


def closes_slowly_after_term(ready: Path, close_s: float) -> str:
    """A unit that handles TERM by closing for `close_s` before it exits, like a bounded drain."""
    return (
        "import pathlib,signal,sys,time\n"
        "def close(*_):\n"
        f"    time.sleep({close_s}); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, close)\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "time.sleep(60)\n"
    )


async def test_unit_closing_past_the_default_window_stops_inside_its_declared_window(
    tmp_path: Path,
) -> None:
    """A unit still closing inside its own declared bound is stopped, not refused.

    Scaled from production: root's default window is 0.2 s here (10 s there) and
    the unit closes for 0.8 s; its manifest declares a 3 s window.
    """
    ready = tmp_path / "ready"
    owner = root(tmp_path, closes_slowly_after_term(ready, 0.8), stop_timeout_s=3.0)
    await owner.start()
    await wait_file(ready)
    result = await owner.down("worker")
    assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
    assert await state(owner) == "stopped"
    assert not list((tmp_path / "custody").iterdir())
    await owner.shutdown()


async def test_unit_declaring_no_window_keeps_the_default_and_the_refusal_names_it(
    tmp_path: Path,
) -> None:
    """Without a declaration the default window applies; a real overrun is still refused."""
    ready = tmp_path / "ready"
    owner = root(tmp_path, closes_slowly_after_term(ready, 0.8))
    await owner.start()
    await wait_file(ready)
    try:
        with pytest.raises(RuntimeError, match=r"did not stop within its 0\.2s window"):
            await owner.down("worker")
        assert (tmp_path / "custody/worker.json").exists()
        for _ in range(100):  # the unit finishes closing on its own
            if await state(owner) == "stopped":
                break
            await asyncio.sleep(0.02)
        await owner.down("worker")  # the retry the operator had to make: nothing is signalled
        assert not list((tmp_path / "custody").iterdir())
    finally:
        await owner.shutdown()


async def test_force_escalates_after_the_units_declared_window(tmp_path: Path) -> None:
    """Explicit force still kills, but only once the unit's own window has passed."""
    ready = tmp_path / "ready"
    ignores_term = (
        "import signal,pathlib,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    owner = root(tmp_path, ignores_term, stop_timeout_s=0.8)
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
    """The incident at production scale: root's default 10 s window, a unit closing for 12.5 s.

    A unit still closing inside its own bound used to end as `internal: internal
    error` on the client; with the unit's declared window the same `down` is ok.
    """
    ready = short_tmp / "ready"
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", "-c", closes_slowly_after_term(ready, 12.5)),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=20.0,
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=short_tmp)  # the default SupervisorConfig
    server = ControlServer(short_tmp / "ava-root.sock", owner.dispatch)
    await server.start()
    try:
        await owner.start()
        await wait_file(ready)
        response = await asyncio.to_thread(
            RootClient(short_tmp / "ava-root.sock", timeout=60).down, "worker"
        )
        assert response["ok"] is True, response
        assert await state(owner) == "stopped"
        assert not list((short_tmp / "custody").iterdir())
    finally:
        await owner.shutdown()
        await server.close()
