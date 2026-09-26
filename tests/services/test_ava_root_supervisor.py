"""Root ownership against real disposable processes; no service/helper jobs."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from services.ava_root.inputs import InputSeal
from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.supervisor import Supervisor, SupervisorConfig


def root(tmp_path: Path, code: str, *, env: tuple[tuple[str, str], ...] = ()) -> Supervisor:
    unit = UnitManifest(
        "worker", (sys.executable, "-u", "-c", code), RestartPolicy.ALWAYS, "root", env
    )
    return Supervisor(
        UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig(stop_timeout_s=0.2)
    )


async def row(owner: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]


async def wait_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"child did not write {path}")


async def test_parent_birth_and_private_environment_are_retained(tmp_path: Path) -> None:
    output = tmp_path / "environment"
    owner = root(
        tmp_path,
        f"import os,pathlib,time; pathlib.Path({str(output)!r}).write_text(os.environ['ROOT_TEST']); time.sleep(60)",
        env=(("ROOT_TEST", "projected"),),
    )
    await owner.start()
    try:
        await wait_file(output)
        unit = await row(owner)
        assert psutil.Process(unit["pid"]).ppid() == os.getpid()
        assert isinstance(unit["create_time"], float)
        assert output.read_text() == "projected"
        assert "projected" not in str(await owner.status())
        assert (tmp_path / "custody/worker.json").exists()
    finally:
        await owner.shutdown()
    assert not list((tmp_path / "custody").iterdir())


async def test_up_is_idempotent_and_restart_stops_previous_generation(tmp_path: Path) -> None:
    owner = root(tmp_path, "import time; time.sleep(60)")
    await owner.start()
    try:
        original = (await row(owner))["pid"]
        await owner.up("worker")
        assert (await row(owner))["pid"] == original
        await owner.restart("worker")
        assert (await row(owner))["pid"] != original
        assert not psutil.pid_exists(original)
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("change", ["bytes", "added-file", "removed-file"])
async def test_restart_refuses_changed_inputs_before_native_birth(
    tmp_path: Path, change: str
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    value = config / "value"
    value.write_text("admitted")
    output = tmp_path / "read-config"
    unit = UnitManifest(
        "worker",
        (
            sys.executable,
            "-c",
            f"import pathlib,time; pathlib.Path({str(output)!r}).write_text(pathlib.Path({str(value)!r}).read_text()); time.sleep(60)",
        ),
        RestartPolicy.ALWAYS,
        "root",
        inputs=(InputSeal.capture(config),),
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path / "root")
    await owner.start()
    try:
        await wait_file(output)
        assert output.read_text() == "admitted"
        original = (await row(owner))["pid"]
        if change == "bytes":
            value.write_text("unapproved")
        elif change == "added-file":
            (config / "new-rule").write_text("unapproved")
        else:
            value.unlink()
        output.unlink()
        await owner.restart("worker")
        current = await row(owner)
        assert current["state"] == "stopped"
        assert "service input changed" in current["last_error"]
        assert not psutil.pid_exists(original)
        assert not output.exists()
        assert not list((tmp_path / "root/custody").glob("*.json"))
        value.write_text("admitted")
        (config / "new-rule").unlink(missing_ok=True)
        await owner.up("worker")
        await wait_file(output)
        assert output.read_text() == "admitted"
    finally:
        await owner.shutdown()


async def test_graceful_timeout_retains_scope_until_explicit_force(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    owner = root(
        tmp_path,
        f"import signal,pathlib,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
    )
    await owner.start()
    await wait_file(ready)
    pid = (await row(owner))["pid"]
    try:
        with pytest.raises(RuntimeError, match="ownership retained"):
            await owner.down("worker")
        assert psutil.pid_exists(pid)
        assert (tmp_path / "custody/worker.json").exists()
    finally:
        await owner.down("worker", force=True)
        await owner.shutdown()
    assert not psutil.pid_exists(pid)
    assert not list((tmp_path / "custody").iterdir())


async def test_stop_closes_captured_descendant_after_leader_exits(tmp_path: Path) -> None:
    child_file = tmp_path / "child"
    code = f"import subprocess,sys,pathlib,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path({str(child_file)!r}).write_text(str(child.pid)); time.sleep(60)"
    owner = root(tmp_path, code)
    await owner.start()
    await wait_file(child_file)
    child = psutil.Process(int(child_file.read_text()))
    await owner.down("worker")
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    assert not list((tmp_path / "custody").iterdir())
    await owner.shutdown()


async def test_unexpected_exit_cannot_authorize_cold_duplicate(tmp_path: Path) -> None:
    owner = root(tmp_path, "import time; time.sleep(0.05)")
    await owner.start()
    await asyncio.sleep(0.15)
    status = await row(owner)
    assert status["pid"] is None
    assert "custody" in status["last_error"]
    other = root(tmp_path, "raise AssertionError('must not spawn')")
    with pytest.raises(RuntimeError, match="custody requires reconciliation"):
        await other.start()
    with pytest.raises(RuntimeError, match="exited before scope capture"):
        await owner.shutdown()
    assert (tmp_path / "custody/worker.json").exists()


async def test_unacknowledged_intent_blocks_launch_without_signals(tmp_path: Path) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    intent = directory / "worker.json"
    intent.write_text('{"stage":"spawning"}')
    owner = root(tmp_path, "raise AssertionError('must not spawn')")
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        await owner.start()
    assert intent.read_text() == '{"stage":"spawning"}'
