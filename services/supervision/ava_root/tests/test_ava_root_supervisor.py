"""Root ownership against real disposable processes; no service/helper jobs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from functools import partial
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


def exits_on(trigger: Path, before: str = "pass") -> str:
    """Unit code that runs `before`, then exits once the test creates `trigger`.

    A unit that exits at once can be reaped before root reads its birth; these
    tests inspect or rewrite that birth, so each creates `trigger` only after
    `start()` has returned.
    """
    return (
        f"import pathlib,sys,time\n{before}\n"
        "deadline=time.monotonic()+30\n"
        f"while not pathlib.Path({str(trigger)!r}).exists():\n"
        "    if time.monotonic()>deadline: sys.exit(1)\n"
        "    time.sleep(0.01)\n"
    )


async def exited(owner: Supervisor) -> None:
    """Wait until root's watch task has processed the unit's own exit."""
    for _ in range(100):
        if (await row(owner))["state"] == "stopped":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("unit did not exit on its own")


async def test_stop_after_unexpected_exit_closes_surviving_descendants(tmp_path: Path) -> None:
    child_file, trigger = tmp_path / "child", tmp_path / "go"
    spawn = f"import subprocess; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path({str(child_file)!r}).write_text(str(child.pid))"
    owner = root(tmp_path, exits_on(trigger, spawn))
    await owner.start()
    child = psutil.Process(await pid_in(child_file))
    try:
        trigger.touch()
        await exited(owner)
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


def kill_all(spawned: list[psutil.Process]) -> None:
    """Kill each test child and everything it forked, so a failed assertion leaks none.

    psutil refuses to signal a PID that now names another birth.
    """
    processes = list(spawned)
    for process in spawned:
        with contextlib.suppress(psutil.NoSuchProcess):
            processes.extend(process.children(recursive=True))
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()


async def gone(process: psutil.Process, *, reaped: bool = False) -> None:
    """Wait for `process` to end; `reaped` waits until it left the process table (a zombie fills its group)."""
    for _ in range(250):
        if not process.is_running() if reaped else ended(process):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"pid {process.pid} did not exit")


async def test_exited_birth_at_a_reused_pid_never_signals_the_stranger(tmp_path: Path) -> None:
    """The recorded PID now belongs to another birth leading its own group.

    The recorded birth is positively dead, so its custody is released; the
    stranger is never signalled, captured, or adopted.
    """
    trigger = tmp_path / "go"
    owner = root(tmp_path, exits_on(trigger))
    await owner.start()
    trigger.touch()
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
        generation.custody.retain(generation.tracked, generation.proc.pid)
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
    trigger = tmp_path / "leader-go"
    spawn = f"import subprocess; subprocess.Popen([sys.executable,'-c',{survivor!r}])"
    stays = f"import sys,time; {spawn}; time.sleep(60)"
    owner = root(tmp_path, exits_on(trigger, spawn) if reaped == "unexpected-exit" else stays)
    await owner.start()
    survivor_process = psutil.Process(await pid_in(survivor_file))
    try:
        trigger.touch()
        if reaped == "refused-stop":
            with pytest.raises(RuntimeError, match="ownership retained"):
                await owner.down("worker")
        await exited(owner)
    except BaseException:
        kill_all([survivor_process])
        raise
    return owner, survivor_process


async def stranger_group(
    tmp_path: Path, *, own_session: bool = False
) -> tuple[int, psutil.Process, Path]:
    """Another program's group whose leader exited like a classic daemon.

    The group forms in this process's session, which is root's in these tests;
    with `own_session` its leader calls setsid() first (fork, setsid, fork).
    Returns its number (no process holds it as a PID), its live member, and the
    file where that member logs any stop signal it receives.
    """
    signals, ready, member_file = (tmp_path / name for name in ("signals", "ready", "member"))
    daemon = (
        "import pathlib,subprocess,sys; "
        f"m=subprocess.Popen([sys.executable,'-c',{signal_recorder(signals, ready)!r}]); "
        f"pathlib.Path({str(member_file)!r}).write_text(str(m.pid))"
    )
    stranger = subprocess.Popen(  # noqa: S603 — disposable test child
        [sys.executable, "-c", daemon],
        start_new_session=own_session,
        process_group=None if own_session else 0,
    )
    assert stranger.wait(timeout=10) == 0, "the stranger's leader must exit and be reaped"
    member = psutil.Process(await pid_in(member_file))
    try:
        await wait_file(ready)
        assert os.getpgid(member.pid) == stranger.pid
        assert (os.getsid(member.pid) != os.getsid(0)) is own_session
    except BaseException:
        kill_all([member])
        raise
    return stranger.pid, member, signals


async def stranger_at_number(
    tmp_path: Path, reaped: str, *, own_session: bool
) -> tuple[Supervisor, int, psutil.Process, Path]:
    """A unit reaped with a survivor that has since exited; a stranger's group carries its number.

    The kernel may hand the unit's group number to another program once its
    survivors exit; the recorded leader is pointed at the stranger's number as
    if it had. Returns root, that number, the stranger's member and its signal log.
    """
    owner, survivor = await reaped_with_survivor(tmp_path, reaped)
    try:
        generation = owner._units["worker"].generation
        assert generation is not None and generation.identity is not None
        assert generation.scope_closed_at_exit is False
        survivor.kill()
        await gone(survivor, reaped=True)
        pgid, member, signals = await stranger_group(tmp_path, own_session=own_session)
    except BaseException:
        kill_all([survivor])
        raise
    dead = generation.identity
    generation.identity = OwnedProcess(pgid, dead.birth, dead.starttime)
    return owner, pgid, member, signals


@pytest.mark.parametrize("reaped", ["unexpected-exit", "refused-stop"])
async def test_exited_leader_never_signals_a_stranger_group_at_its_number(
    tmp_path: Path, reaped: str
) -> None:
    """The unit's group ended after its leader's reap; its number now names another program's group.

    That group lies in root's own session, so root cannot tell it from the
    unit's. Whether the unit's leader exited on its own or during a refused
    stop, no later stop signals by that number: nothing is signalled, even with
    force, and custody stays with a refusal that names the group and the record.
    """
    owner, pgid, member, signals = await stranger_at_number(tmp_path, reaped, own_session=False)
    record = tmp_path / "custody/worker.json"
    try:
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
        kill_all([member])
    await gone(member, reaped=True)
    # Once that group has ended, the stop proves the unit's group over.
    await owner.down("worker")
    assert not list((tmp_path / "custody").iterdir())
    await owner.shutdown()


@pytest.mark.parametrize("reaped", ["unexpected-exit", "refused-stop"])
async def test_a_group_in_another_session_at_the_number_is_released_without_a_signal(
    tmp_path: Path, reaped: str
) -> None:
    """A classic daemon (fork, setsid, fork) holds the unit's group number.

    Its group lies in another session, so it holds no process of the unit:
    the stop releases custody without signalling it, and root's own shutdown,
    which stops the tree the same way, completes.
    """
    owner, _pgid, member, signals = await stranger_at_number(tmp_path, reaped, own_session=True)
    try:
        await owner.down("worker")
        assert owner._units["worker"].generation is None
        assert (await row(owner))["last_error"] is None
        assert not list((tmp_path / "custody").iterdir())
        await owner.shutdown()
        assert not signals.exists(), "the stranger's member received a signal"
        assert not ended(member)
    finally:
        kill_all([member])


async def test_a_group_whose_session_root_cannot_read_keeps_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root cannot read the session of the group at the unit's number, so it refuses.

    The stranger receives nothing; once its session reads again, the stop releases.
    """
    owner, pgid, member, signals = await stranger_at_number(
        tmp_path, "unexpected-exit", own_session=True
    )
    getsid = os.getsid

    def denied(pid: int) -> int:
        if pid == member.pid:
            raise PermissionError(f"session of {pid} not readable")
        return getsid(pid)

    try:
        monkeypatch.setattr(os, "getsid", denied)
        with pytest.raises(RuntimeError, match=f"process group {pgid}"):
            await owner.down("worker")
        assert (tmp_path / "custody/worker.json").exists()
        monkeypatch.undo()
        await owner.down("worker")
        assert not list((tmp_path / "custody").iterdir())
        assert not signals.exists(), "the stranger's member received a signal"
        assert not ended(member)
    finally:
        kill_all([member])
    await owner.shutdown()


@pytest.mark.parametrize(
    ("read", "own_session"),
    [("outside-the-group", False), ("setsid-between-reads", False), ("birth-changed", True)],
)
async def test_only_one_birth_read_inside_the_group_proves_another_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str, own_session: bool
) -> None:
    """Reads that do not place one birth inside the group prove nothing.

    The member's reads are faked: its PID now names a process of another
    session outside the group; it calls setsid() between root's reads of its
    session and its group; or its PID names another birth once both are read.
    Each time the stop refuses and signals nothing.
    """
    owner, pgid, member, signals = await stranger_at_number(
        tmp_path, "unexpected-exit", own_session=own_session
    )
    getsid, getpgid, live = os.getsid, os.getpgid, OwnedProcess.live
    reads: list[str] = []

    def placement(kind: str, pid: int) -> int:
        real = getsid(pid) if kind == "session" else getpgid(pid)
        if pid != member.pid:
            return real
        reads.append(kind)
        if read == "outside-the-group":
            return 1
        if read == "setsid-between-reads" and len(reads) > 1:
            return member.pid  # it now leads a session and a group of its own
        return real

    def reborn(identity: OwnedProcess) -> bool:
        return identity.pid != member.pid and live(identity)

    try:
        monkeypatch.setattr(os, "getsid", partial(placement, "session"))
        monkeypatch.setattr(os, "getpgid", partial(placement, "group"))
        if read == "birth-changed":
            monkeypatch.setattr(OwnedProcess, "live", reborn)
        with pytest.raises(RuntimeError, match=f"process group {pgid}"):
            await owner.down("worker")
        monkeypatch.undo()
        assert reads, "root never read the member"
        assert not signals.exists(), "the stranger's member received a signal"
        assert not ended(member)
    finally:
        kill_all([member])
    await gone(member, reaped=True)
    await owner.down("worker")
    await owner.shutdown()


async def test_moving_the_record_aside_settles_an_unproven_group_without_a_signal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal's escape works on the running root, which keeps its generation in memory.

    A stranger's group in root's session holds the unit's number, so the stop
    refuses. Once the operator moves the record aside with no recorded birth
    alive, the retried stop drops the generation, the stranger receives
    nothing, and root's own shutdown completes.
    """
    owner, pgid, member, signals = await stranger_at_number(
        tmp_path, "unexpected-exit", own_session=False
    )
    try:
        with pytest.raises(RuntimeError, match=f"process group {pgid}"):
            await owner.down("worker")
        (tmp_path / "custody/worker.json").rename(tmp_path / "worker.json.aside")
        await owner.down("worker")
        assert owner._units["worker"].generation is None
        released = await row(owner)
        assert released["state"] == "stopped" and released["last_error"] is None
        assert "operator moved the custody record aside" in caplog.text
        await owner.shutdown()
        assert not signals.exists(), "the stranger's member received a signal"
        assert not ended(member)
    finally:
        kill_all([member])


async def test_a_moved_aside_record_never_drops_a_live_recorded_birth(tmp_path: Path) -> None:
    """Without its record, root cannot record custody before a signal, so it refuses.

    Even with force, the recorded survivor keeps running and the generation
    stays; restoring the record lets the explicit force stop close it.
    """
    owner, survivor = await reaped_with_survivor(tmp_path, "unexpected-exit")
    record, aside = tmp_path / "custody/worker.json", tmp_path / "worker.json.aside"
    try:
        record.rename(aside)
        for force in (False, True):
            with pytest.raises(RuntimeError, match="moved aside") as refused:
                await owner.down("worker", force=force)
            assert str(survivor.pid) in str(refused.value)
        assert not ended(survivor)
        assert owner._units["worker"].generation is not None
        aside.rename(record)
        await owner.down("worker", force=True)
        await gone(survivor)
        assert not list((tmp_path / "custody").iterdir())
    finally:
        kill_all([survivor])
    await owner.shutdown()


async def test_unobserved_reap_refuses_until_the_record_is_moved_aside(tmp_path: Path) -> None:
    trigger = tmp_path / "go"
    owner = root(tmp_path, exits_on(trigger))
    await owner.start()
    trigger.touch()
    await exited(owner)
    generation = owner._units["worker"].generation
    assert generation is not None
    generation.exited.clear()  # as if root never observed its leader's reap
    record = tmp_path / "custody/worker.json"
    with pytest.raises(RuntimeError, match="never observed its reap") as refused:
        await owner.down("worker")
    assert str(record) in str(refused.value)
    record.rename(tmp_path / "worker.json.aside")
    await owner.down("worker")
    assert owner._units["worker"].generation is None
    await owner.shutdown()


async def test_a_leader_reaped_before_its_birth_read_is_stopped_by_what_its_reap_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child watcher reaps the leader before root reads its birth.

    The stop closes the child recorded at the reap; the other, unreadable then,
    is never signalled, and the stop refuses until the record is moved aside."""
    spawn, capture = asyncio.create_subprocess_exec, OwnedProcess.capture

    async def reaped_first(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await spawn(*args, **kwargs)
        await proc.wait()
        return proc

    def unreadable(_cls: type[OwnedProcess], process: psutil.Process) -> OwnedProcess:
        if process.pid == int((tmp_path / "other").read_text()):
            raise psutil.AccessDenied(process.pid)
        return capture(process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reaped_first)
    monkeypatch.setattr(OwnedProcess, "capture", classmethod(unreadable))
    code = f"import pathlib,subprocess,sys\nfor n in ('kept', 'other'): c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path({str(tmp_path)!r}, n).write_text(str(c.pid))"
    owner = root(tmp_path, code)
    await owner.start()
    spawned = [psutil.Process(await pid_in(tmp_path / name)) for name in ("kept", "other")]
    try:
        kept, other = spawned
        await exited(owner)
        generation = owner._units["worker"].generation
        assert generation is not None and generation.identity is None
        assert {item.pid for item in generation.tracked} == {kept.pid}
        refusal = rf"process group {generation.proc.pid} still holds pids \[{other.pid}\]"
        with pytest.raises(RuntimeError, match=refusal):
            await owner.down("worker")
        assert ended(kept) and not ended(other)
        (tmp_path / "custody/worker.json").rename(tmp_path / "worker.json.aside")
        await owner.down("worker")
        await owner.shutdown()
        assert not ended(other), "the unrecorded child received a signal"
    finally:
        kill_all(spawned)


@pytest.mark.parametrize("moves_group", [False, True])
async def test_exited_leader_stop_closes_recorded_survivors_and_their_later_children(
    tmp_path: Path, moves_group: bool
) -> None:
    """A survivor recorded at the reap is closed with a child it forked after that reap.

    The stop reaches both through the survivor's recorded birth, not through
    the group number, so it still closes them after the survivor moved to a
    group of its own; then custody is released and no error remains.
    """
    survivor_file, trigger, late_file, leader_go = (
        tmp_path / name for name in ("survivor", "go", "late", "leader-go")
    )
    survivor = (
        "import os,pathlib,subprocess,sys,time\n"
        f"pathlib.Path({str(survivor_file)!r}).write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+30\n"
        f"while not pathlib.Path({str(trigger)!r}).exists():\n"
        "    if time.monotonic()>deadline: sys.exit(1)\n"
        "    time.sleep(0.01)\n"
        + ("os.setpgid(0, 0)\n" if moves_group else "")
        + "late=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(late_file)!r}).write_text(str(late.pid))\n"
        "time.sleep(60)\n"
    )
    spawn = f"import subprocess; subprocess.Popen([sys.executable,'-c',{survivor!r}])"
    owner = root(tmp_path, exits_on(leader_go, spawn))
    await owner.start()
    spawned: list[psutil.Process] = []
    try:
        survivor_process = psutil.Process(await pid_in(survivor_file))
        spawned.append(survivor_process)
        leader_go.touch()
        await exited(owner)
        generation = owner._units["worker"].generation
        assert generation is not None and generation.scope_closed_at_exit is False
        assert survivor_process.pid in {item.pid for item in generation.tracked}
        trigger.touch()
        late = psutil.Process(await pid_in(late_file))
        spawned.append(late)
        await owner.down("worker")
        assert ended(survivor_process) and ended(late)
        assert not list((tmp_path / "custody").iterdir())
        assert (await row(owner))["last_error"] is None
    finally:
        kill_all(spawned)
    await owner.shutdown()


@pytest.mark.parametrize("unreadable", ["access-denied", "no-start-ticks"])
async def test_an_unreadable_survivor_leaves_its_siblings_recorded_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    """One member's birth cannot be read at the reap; its sibling is still recorded.

    The stop closes the recorded sibling. The unreadable member is never
    signalled: it keeps the group occupied, so the stop refuses, naming it alone.
    """
    kept_file, denied_file, trigger = (tmp_path / name for name in ("kept", "denied", "go"))
    child = "import time; time.sleep(60)"
    owner = root(
        tmp_path,
        "import pathlib,subprocess,sys,time\n"
        f"kept=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        f"denied=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        f"pathlib.Path({str(kept_file)!r}).write_text(str(kept.pid))\n"
        f"pathlib.Path({str(denied_file)!r}).write_text(str(denied.pid))\n"
        "deadline=time.monotonic()+30\n"
        f"while not pathlib.Path({str(trigger)!r}).exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n",
    )
    await owner.start()
    spawned: list[psutil.Process] = []
    try:
        kept = psutil.Process(await pid_in(kept_file))
        denied = psutil.Process(await pid_in(denied_file))
        spawned += [kept, denied]
        capture = OwnedProcess.capture

        def deny_one(_cls: type[OwnedProcess], process: psutil.Process) -> OwnedProcess:
            if process.pid != denied.pid:
                return capture(process)
            if unreadable == "access-denied":
                raise psutil.AccessDenied(process.pid)
            raise RuntimeError(f"cannot capture Linux start ticks for PID {process.pid}")

        monkeypatch.setattr(OwnedProcess, "capture", classmethod(deny_one))
        trigger.touch()
        await exited(owner)
        monkeypatch.undo()
        generation = owner._units["worker"].generation
        assert generation is not None and generation.scope_closed_at_exit is False
        recorded = {item.pid for item in generation.tracked}
        assert kept.pid in recorded and denied.pid not in recorded
        with pytest.raises(RuntimeError, match="process group") as refused:
            await owner.down("worker")
        assert ended(kept) and not ended(denied)
        assert f"pids [{denied.pid}]" in str(refused.value)
        denied.kill()
        await gone(denied, reaped=True)
        await owner.down("worker")
        assert not list((tmp_path / "custody").iterdir())
    finally:
        kill_all(spawned)
    await owner.shutdown()


async def test_unconfirmable_exited_birth_retains_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = tmp_path / "go"
    owner = root(tmp_path, exits_on(trigger))
    await owner.start()
    trigger.touch()
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
