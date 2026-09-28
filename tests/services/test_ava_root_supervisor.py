"""Root ownership against real disposable processes; no service/helper jobs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from services.ava_root.inputs import InputSeal
from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.supervisor import Supervisor, SupervisorConfig
from shared.native_process.ownership import OwnedProcess


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


async def exited(owner: Supervisor) -> None:
    """Wait until root's watch task has processed the unit's own exit."""
    for _ in range(100):
        if (await row(owner))["state"] == "stopped":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("unit did not exit on its own")


async def test_unexpected_exit_cannot_authorize_cold_duplicate(tmp_path: Path) -> None:
    owner = root(tmp_path, "import time; time.sleep(0.05)")
    await owner.start()
    await exited(owner)
    status = await row(owner)
    assert "custody" in status["last_error"]
    other = root(tmp_path, "raise AssertionError('must not spawn')")
    with pytest.raises(RuntimeError, match="custody requires reconciliation"):
        await other.start()
    assert (tmp_path / "custody/worker.json").exists()
    # The owner reaped that exact birth and its group was empty: stop settles it.
    await owner.down("worker")
    released = await row(owner)
    assert released["state"] == "stopped"
    assert released["last_error"] is None, "the settled record still asks for reconciliation"
    assert not list((tmp_path / "custody").iterdir())
    await owner.shutdown()


async def test_stop_after_unexpected_exit_closes_surviving_descendants(tmp_path: Path) -> None:
    child_file = tmp_path / "child"
    code = f"import subprocess,sys,pathlib; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path({str(child_file)!r}).write_text(str(child.pid))"
    owner = root(tmp_path, code)
    await owner.start()
    await wait_file(child_file)
    await exited(owner)
    child = psutil.Process(int(child_file.read_text()))
    try:
        assert child.is_running()
        await owner.down("worker")
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert not list((tmp_path / "custody").iterdir())
    finally:
        if child.is_running():
            child.kill()
    await owner.shutdown()


def signal_recorder(signals: Path, ready: Path) -> str:
    """A stranger's code: log every catchable stop signal instead of exiting."""
    return (
        "import pathlib,signal,time\n"
        f"log=pathlib.Path({str(signals)!r})\n"
        "def record(number, _frame):\n"
        "    with log.open('a') as out: out.write(f'{number}\\n')\n"
        "for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):\n"
        "    signal.signal(number, record)\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "time.sleep(60)\n"
    )


async def pid_in(path: Path) -> int:
    """The PID a child writes to `path`, once the write is complete."""
    for _ in range(250):
        with contextlib.suppress(FileNotFoundError, ValueError):
            return int(path.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError(f"no PID written to {path}")


def ended(process: psutil.Process) -> bool:
    """Whether `process` exited; a zombie counts, since its reaper is not this test."""
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def gone(process: psutil.Process) -> None:
    for _ in range(250):
        if ended(process):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"pid {process.pid} did not exit")


async def test_exited_birth_at_a_reused_pid_never_signals_the_stranger(tmp_path: Path) -> None:
    """The recorded PID now belongs to another birth leading its own group.

    The recorded birth is positively dead, so its custody is released; the
    stranger is never signalled, captured, or adopted.
    """
    owner = root(tmp_path, "pass")
    await owner.start()
    await exited(owner)
    signals, ready = tmp_path / "stranger-signals", tmp_path / "stranger-ready"
    code = signal_recorder(signals, ready)
    stranger = subprocess.Popen([sys.executable, "-c", code], process_group=0)  # noqa: S603 — disposable test child
    try:
        await wait_file(ready)
        generation = owner._units["worker"].generation
        assert generation is not None and generation.identity is not None
        assert generation.custody is not None
        dead = generation.identity
        # The kernel handed the recorded PID to the stranger; the record keeps
        # the dead birth. Survivors at reap would otherwise send the stop to
        # the group that now carries that number.
        generation.identity = OwnedProcess(stranger.pid, dead.birth, dead.starttime)
        generation.tracked = {generation.identity}
        generation.custody.retain(generation.tracked)
        generation.scope_closed_at_exit = False
        await owner.down("worker")
        assert stranger.poll() is None
        assert not signals.exists(), "the stranger received a signal"
        assert not list((tmp_path / "custody").iterdir())
    finally:
        stranger.kill()
        stranger.wait(timeout=5)
    await owner.shutdown()


async def reaped_with_survivor(tmp_path: Path, reaped: str) -> tuple[Supervisor, psutil.Process]:
    """A unit whose leader root reaped while a TERM-ignoring survivor lived on.

    The leader exits on its own, or during a stop that the survivor makes refuse.
    """
    survivor_file = tmp_path / "survivor"
    survivor = (
        "import os,pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(survivor_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader_stays = "" if reaped == "unexpected-exit" else "; time.sleep(60)"
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{survivor!r}]){leader_stays}"
    owner = root(tmp_path, code)
    await owner.start()
    survivor_process = psutil.Process(await pid_in(survivor_file))
    if reaped == "refused-stop":
        with pytest.raises(RuntimeError, match="ownership retained"):
            await owner.down("worker")
    await exited(owner)
    return owner, survivor_process


async def stranger_group(tmp_path: Path) -> tuple[int, psutil.Process, Path]:
    """Another program's group whose leader exited like a classic daemon.

    Returns its number (no process holds it as a PID), its live member, and the
    file where that member logs any stop signal it receives.
    """
    signals, ready, member_file = (tmp_path / name for name in ("signals", "ready", "member"))
    daemon = (
        "import pathlib,subprocess,sys; "
        f"m=subprocess.Popen([sys.executable,'-c',{signal_recorder(signals, ready)!r}]); "
        f"pathlib.Path({str(member_file)!r}).write_text(str(m.pid))"
    )
    stranger = subprocess.Popen([sys.executable, "-c", daemon], process_group=0)  # noqa: S603 — disposable test child
    assert stranger.wait(timeout=10) == 0, "the stranger's leader must exit and be reaped"
    member = psutil.Process(await pid_in(member_file))
    await wait_file(ready)
    assert os.getpgid(member.pid) == stranger.pid
    return stranger.pid, member, signals


@pytest.mark.parametrize("reaped", ["unexpected-exit", "refused-stop"])
async def test_exited_leader_never_signals_a_stranger_group_at_its_number(
    tmp_path: Path, reaped: str
) -> None:
    """The unit's group ended after its leader's reap; its number now names another program's group.

    Whether the unit's leader exited on its own or during a refused stop, no
    later stop lists that group: nothing is signalled, even with force, and
    custody stays with a refusal that names the group and the record.
    """
    owner, survivor = await reaped_with_survivor(tmp_path, reaped)
    generation = owner._units["worker"].generation
    assert generation is not None and generation.identity is not None
    assert generation.scope_closed_at_exit is False
    survivor.kill()
    await gone(survivor)
    pgid, member, signals = await stranger_group(tmp_path)
    record = tmp_path / "custody/worker.json"
    try:
        # The number the stop would read now names the stranger's group.
        dead = generation.identity
        generation.identity = OwnedProcess(pgid, dead.birth, dead.starttime)
        for force in (False, True):
            with pytest.raises(RuntimeError) as refused:
                await owner.down("worker", force=force)
            assert not signals.exists(), "the stranger's member received a signal"
            message = str(refused.value)
            for named in (f"process group {pgid}", str(member.pid), str(record), "retry the stop"):
                assert named in message
        assert not ended(member)
        assert record.exists()
    finally:
        member.kill()
        await gone(member)
    # Once that group has ended, the stop proves the unit's group over.
    await owner.down("worker")
    assert not list((tmp_path / "custody").iterdir())
    await owner.shutdown()


@pytest.mark.parametrize("moves_group", [False, True])
async def test_exited_leader_stop_closes_recorded_survivors_and_their_later_children(
    tmp_path: Path, moves_group: bool
) -> None:
    """A survivor recorded at the reap is closed with a child it forked after that reap.

    The stop reaches both through the survivor's recorded birth, not through
    the group number, so it still closes them after the survivor moved to a
    group of its own; then custody is released and no error remains.
    """
    survivor_file, trigger, late_file = (tmp_path / name for name in ("survivor", "go", "late"))
    survivor = (
        "import os,pathlib,subprocess,sys,time\n"
        f"pathlib.Path({str(survivor_file)!r}).write_text(str(os.getpid()))\n"
        f"while not pathlib.Path({str(trigger)!r}).exists(): time.sleep(0.01)\n"
        + ("os.setpgid(0, 0)\n" if moves_group else "")
        + "late=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(late_file)!r}).write_text(str(late.pid))\n"
        "time.sleep(60)\n"
    )
    owner = root(
        tmp_path, f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{survivor!r}])"
    )
    await owner.start()
    survivor_process = psutil.Process(await pid_in(survivor_file))
    await exited(owner)
    generation = owner._units["worker"].generation
    assert generation is not None and generation.scope_closed_at_exit is False
    assert survivor_process.pid in {item.pid for item in generation.tracked}
    trigger.touch()
    late = psutil.Process(await pid_in(late_file))
    try:
        await owner.down("worker")
        assert ended(survivor_process) and ended(late)
        assert not list((tmp_path / "custody").iterdir())
        assert (await row(owner))["last_error"] is None
    finally:
        for process in (survivor_process, late):
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
    await owner.shutdown()


async def test_unconfirmable_exited_birth_retains_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = root(tmp_path, "pass")
    await owner.start()
    await exited(owner)

    def unverifiable(self: OwnedProcess) -> bool:
        raise RuntimeError(f"cannot verify process identity for PID {self.pid}")

    monkeypatch.setattr(OwnedProcess, "live", unverifiable)
    record = tmp_path / "custody/worker.json"
    with pytest.raises(RuntimeError) as refused:
        await owner.down("worker")
    message = str(refused.value)
    assert "cannot verify process identity" in message
    assert str(record) in message and "retry" in message
    assert record.exists()
    monkeypatch.undo()
    await owner.shutdown()


async def test_unacknowledged_intent_blocks_launch_without_signals(tmp_path: Path) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    intent = directory / "worker.json"
    intent.write_text('{"stage":"spawning"}')
    owner = root(tmp_path, "raise AssertionError('must not spawn')")
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        await owner.start()
    assert intent.read_text() == '{"stage":"spawning"}'
