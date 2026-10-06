"""Root ownership against real disposable processes; no service/helper jobs."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.native_process.ownership import OwnedProcess
from services.supervision.ava_root.inputs import InputSeal
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig


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
        assert not (tmp_path / "custody").exists()
    finally:
        await owner.shutdown()


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
            # Rename into place: the waiter polls for existence, not content.
            f"import pathlib,time; t=pathlib.Path({str(output)!r} + '.tmp'); t.write_text(pathlib.Path({str(value)!r}).read_text()); t.replace({str(output)!r}); time.sleep(60)",
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


async def test_live_child_timeout_requires_explicit_force(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    owner = root(
        tmp_path,
        f"import signal,pathlib,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
    )
    await owner.start()
    await wait_file(ready)
    pid = (await row(owner))["pid"]
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            await owner.down("worker")
        assert psutil.pid_exists(pid)
        assert not (tmp_path / "custody").exists()
    finally:
        await owner.down("worker", force=True)
        await owner.shutdown()
    assert not psutil.pid_exists(pid)


async def test_normal_stop_signals_the_known_live_group(tmp_path: Path) -> None:
    child_file = tmp_path / "child"
    code = (
        "import subprocess,sys,pathlib,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_file)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    owner = root(tmp_path, code)
    await owner.start()
    await wait_file(child_file)
    child = psutil.Process(int(child_file.read_text()))
    try:
        assert os.getpgid(child.pid) == (await row(owner))["pid"]
        assert os.getsid(child.pid) == os.getsid(0)
        await owner.down("worker")
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()
        await owner.shutdown()


async def test_unexpected_exit_can_be_replaced_without_custody_reconciliation(
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "exit"
    owner = root(
        tmp_path,
        "import pathlib,time; "
        f"p=pathlib.Path({str(trigger)!r}); "
        "exec('while not p.exists(): time.sleep(0.01)')",
    )
    await owner.start()
    old = (await row(owner))["pid"]
    trigger.touch()
    generation = owner._units["worker"].generation
    assert generation is not None
    await asyncio.wait_for(generation.exited.wait(), 5)
    assert owner._units["worker"].generation is None
    assert owner.revival_deferral("worker") is None
    trigger.unlink()
    try:
        await owner.up("worker")
        assert (await row(owner))["pid"] != old
        assert (await row(owner))["state"] == "running"
    finally:
        await owner.shutdown()


def test_changed_birth_never_signals_a_numeric_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.supervision.ava_root.stopping import StoppingMixin

    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    try:
        real = OwnedProcess.capture(psutil.Process(stranger.pid))
        stale = OwnedProcess(
            real.pid, real.birth - 1000, None if real.starttime is None else real.starttime - 1
        )

        def forbid_group_signal(group: int, signum: int) -> None:
            raise AssertionError("a changed birth must never signal a numeric group")

        monkeypatch.setattr(os, "killpg", forbid_group_signal)
        StoppingMixin._signal_owned(stale, force=False)
        StoppingMixin._signal_owned(stale, force=True)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait(timeout=5)


async def test_obsolete_custody_records_do_not_gate_ordinary_start(tmp_path: Path) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    (directory / "worker.json").write_text("obsolete spawning record")
    owner = root(tmp_path, "import time; time.sleep(60)")
    await owner.start()
    try:
        assert (await row(owner))["state"] == "running"
        assert (await row(owner))["pid"] is not None
    finally:
        await owner.shutdown()


def test_concrete_group_signal_error_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.supervision.ava_root.stopping import StoppingMixin

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=0)
    try:
        identity = OwnedProcess.capture(psutil.Process(child.pid))

        def deny_group_signal(group: int, signum: int) -> None:
            raise PermissionError("known group signal denied")

        monkeypatch.setattr(os, "killpg", deny_group_signal)
        with pytest.raises(PermissionError, match="known group signal denied"):
            StoppingMixin._signal_owned(identity, force=False)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)
