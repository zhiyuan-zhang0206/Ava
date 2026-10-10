"""Real native observations and generation Tasks stay with their original root owner."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from pathlib import Path
from threading import Event
from time import monotonic
from typing import cast

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.ava_root.daemon import _finish
from services.supervision.ava_root.health import HealthMonitor, ProbeRunner
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.probes import ProbeRegistry
from services.supervision.ava_root.server import ControlServer
from services.supervision.ava_root.singleton import acquire_instance_lock, release_instance_lock
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.ava_root.unit_records import _Generation, _UnitRuntime


class WorkerFailure(BaseException):
    """An unknown worker failure outside the ordinary inspection Exception contract."""


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", error)
        return [leaf for child in group.exceptions for leaf in _leaves(child)]
    return [error]


async def _eventually(event: Event) -> None:
    deadline = monotonic() + 2
    while not event.is_set() and monotonic() < deadline:
        await asyncio.sleep(0.001)
    assert event.is_set()


async def _wait_ready(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("test child did not publish readiness")


async def _wait_error(caplog: pytest.LogCaptureFixture, error: BaseException) -> None:
    for _ in range(200):
        if any(record.exc_info and record.exc_info[1] is error for record in caplog.records):
            return
        await asyncio.sleep(0.001)
    raise AssertionError("original watcher error was not immediately visible")


async def _kill_child(generation: _Generation) -> None:
    if generation.proc.returncode is None:
        generation.proc.kill()
    await generation.proc.wait()


async def test_native_worker_error_cannot_skip_other_root_teardowns(short_tmp: Path) -> None:
    runner = ProbeRunner()
    error = WorkerFailure("original native worker error")
    stopped: list[str] = []

    def broken() -> DaemonProbe:
        raise error

    with pytest.raises(WorkerFailure):
        await runner.observe(broken, 0.2)

    class Participant:
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self) -> None: ...

        async def stop(self) -> None:
            stopped.append(self.name)
            if self.name == "probe":
                runner.stop(0.2)

    owner = Supervisor(UnitRegistry([]), run_dir=short_tmp)
    server = ControlServer(short_tmp / "control.sock", owner.dispatch)
    await server.start()
    lock_fd = acquire_instance_lock(short_tmp)
    with pytest.raises(BaseExceptionGroup) as caught:
        await _finish([Participant("peer"), Participant("probe")], server, owner, lock_fd)
    assert _leaves(caught.value) == [error]
    assert stopped == ["probe", "peer"]
    assert not (short_tmp / "control.sock").exists()
    new_lock = acquire_instance_lock(short_tmp)
    release_instance_lock(new_lock)
    with pytest.raises(RuntimeError, match="admission closed"):
        await owner.start()


async def test_late_unknown_worker_error_is_visible_and_raised_by_original_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release, completed = Event(), Event()
    error = WorkerFailure("late original failure")

    def fail_late() -> DaemonProbe:
        try:
            release.wait(3)
            raise error
        finally:
            completed.set()

    runner = ProbeRunner()
    caplog.set_level(logging.ERROR)
    try:
        assert (await runner.observe(fail_late, 0.01)).verdict.value == "unavailable"
        assert runner.stop(0.01) is False
        release.set()
        await _eventually(completed)
        await asyncio.sleep(0.01)
        assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
        with pytest.raises(WorkerFailure) as caught:
            runner.stop(0.2)
        assert caught.value is error
    finally:
        release.set()
        await _eventually(completed)
        with suppress(WorkerFailure):
            runner.stop(0.2)


async def test_waiting_probe_caller_receives_original_unknown_error() -> None:
    error = WorkerFailure("active original failure")

    def broken() -> DaemonProbe:
        raise error

    runner = ProbeRunner()
    try:
        with pytest.raises(WorkerFailure) as caught:
            await runner.observe(broken, 0.2)
        assert caught.value is error
    finally:
        try:
            runner.stop(0.2)
        except WorkerFailure as caught:
            assert caught is error


async def test_cross_generation_watch_errors_are_visible_and_collected_without_peer_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unit = UnitManifest("worker", (sys.executable, "-c", "pass"), RestartPolicy.ALWAYS, "root")
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path)
    original = owner._watch
    errors = [RuntimeError("first generation"), RuntimeError("second generation")]
    watched = 0

    async def fail_after_reap(runtime: _UnitRuntime, generation: _Generation) -> None:
        nonlocal watched
        error = errors[watched]
        watched += 1
        await original(runtime, generation)
        raise error

    monkeypatch.setattr(owner, "_watch", fail_after_reap)
    caplog.set_level(logging.ERROR)
    peer_release = asyncio.Event()
    async with asyncio.TaskGroup() as participants:
        peer = participants.create_task(peer_release.wait())
        try:
            await owner.start()
            for index in range(2):
                if index:
                    await owner.up("worker")
                generation = owner._units["worker"].generation
                assert generation is not None
                done = asyncio.create_task(generation.exited.wait())
                finished, _ = await asyncio.wait({done}, timeout=2)
                assert done in finished, "direct child was not reaped"
                await done
                await asyncio.sleep(0)
                assert generation.proc.returncode is not None
                assert any(
                    record.exc_info and record.exc_info[1] is errors[index]
                    for record in caplog.records
                )
            assert not peer.cancelled() and not peer.done()
            with pytest.raises(ExceptionGroup) as caught:
                await owner.shutdown()
            assert _leaves(caught.value) == errors
            assert not peer.cancelled()
        finally:
            peer_release.set()


async def test_failed_watch_never_certifies_an_unreaped_live_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        RestartPolicy.ALWAYS,
        "root",
    )
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig(stop_timeout_s=0.02)
    )
    error = RuntimeError("wait implementation failed")

    async def broken_watch(_runtime: _UnitRuntime, _generation: _Generation) -> None:
        raise error

    monkeypatch.setattr(owner, "_watch", broken_watch)
    await owner.start()
    generation = owner._units["worker"].generation
    assert generation is not None
    try:
        await asyncio.sleep(0.01)
        assert not generation.exited.is_set()
        assert generation.proc.returncode is None
        stopped = asyncio.create_task(owner.shutdown())
        done, _ = await asyncio.wait({stopped}, timeout=1)
        assert stopped in done
        with pytest.raises(ExceptionGroup) as caught:
            await stopped
        assert error in _leaves(caught.value)
        assert not generation.exited.is_set()
    finally:
        if generation.proc.returncode is None:
            generation.proc.kill()
        await generation.proc.wait()


async def test_shutdown_closes_supervisor_birth_admission(tmp_path: Path) -> None:
    unit = UnitManifest("worker", (sys.executable, "-c", "pass"), RestartPolicy.ALWAYS, "root")
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path)
    await owner.start()
    await owner.shutdown()
    with pytest.raises(RuntimeError, match="admission closed"):
        await owner.up("worker")
    with pytest.raises(RuntimeError, match="admission closed"):
        await owner.restart("worker")
    assert owner._watches == {}


async def test_health_stop_closes_even_unobserved_unit_admission(tmp_path: Path) -> None:
    registry = ProbeRegistry()
    calls = 0

    def fresh() -> DaemonProbe:
        nonlocal calls
        calls += 1
        return DaemonProbe.up("fresh")

    registry.register("unobserved", fresh)
    health = HealthMonitor(Supervisor(UnitRegistry([]), run_dir=tmp_path), registry)
    await health.stop()
    with pytest.raises(RuntimeError, match="admission closed"):
        await health.run_round()
    with pytest.raises(RuntimeError, match="admission closed"):
        await health._probe("unobserved", fresh)
    assert calls == 0


async def test_refused_child_keeps_real_reap_and_late_error_at_original_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ready = tmp_path / "ready"
    code = (
        "import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    unit = UnitManifest("worker", (sys.executable, "-c", code), RestartPolicy.ALWAYS, "root")
    owner = Supervisor(
        UnitRegistry([unit]),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.02, watch_join_timeout_s=0.02),
    )
    original = owner._watch
    error = RuntimeError("late reap bookkeeping failed")

    async def fail_after_reap(runtime: _UnitRuntime, generation: _Generation) -> None:
        await original(runtime, generation)
        raise error

    monkeypatch.setattr(owner, "_watch", fail_after_reap)
    caplog.set_level(logging.ERROR)
    await owner.start()
    generation = owner._units["worker"].generation
    assert generation is not None
    try:
        await _wait_ready(ready)
        stopped = asyncio.create_task(owner.shutdown())
        done, _ = await asyncio.wait({stopped}, timeout=1)
        assert stopped in done, "live-child refusal blocked root's independent exit guard"
        with pytest.raises(ExceptionGroup, match="root shutdown failed"):
            await stopped
        assert owner._units["worker"].generation is generation
        assert len(owner.unfinished_watches) == 1
        assert not owner.unfinished_watches[0].cancelled()
        assert not generation.exited.is_set()
        generation.proc.kill()
        await generation.proc.wait()
        await _wait_error(caplog, error)
        assert generation.exited.is_set()
        assert owner.unfinished_watches == ()
        assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
        with pytest.raises(ExceptionGroup) as caught:
            await owner.shutdown()
        assert _leaves(caught.value) == [error]
    finally:
        await _kill_child(generation)
        if owner.unfinished_watches:
            _, pending = await asyncio.wait(owner.unfinished_watches, timeout=2)
            assert not pending


async def test_watch_join_has_independent_bound_when_completed_child_watch_resists_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = UnitManifest("worker", (sys.executable, "-c", "pass"), RestartPolicy.ALWAYS, "root")
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig(watch_join_timeout_s=0.02)
    )
    original = owner._watch
    release, entered = asyncio.Event(), asyncio.Event()
    error = RuntimeError("late cancel-resistant watch failure")

    async def resistant(runtime: _UnitRuntime, generation: _Generation) -> None:
        await original(runtime, generation)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        raise error

    monkeypatch.setattr(owner, "_watch", resistant)
    await owner.start()
    wait_entered = asyncio.create_task(entered.wait())
    stopped: asyncio.Task[None] | None = None
    try:
        done, _ = await asyncio.wait({wait_entered}, timeout=2)
        assert wait_entered in done, "test child was not really reaped"
        await wait_entered
        stopped = asyncio.create_task(owner.shutdown())
        done, _ = await asyncio.wait({stopped}, timeout=1)
        assert stopped in done, "cancel-resistant work escaped the watcher join budget"
        await stopped
        unfinished = owner.unfinished_watches
        assert len(unfinished) == 1
        release.set()
        await asyncio.wait(unfinished, timeout=1)
        await asyncio.sleep(0)
        assert owner.unfinished_watches == ()
        with pytest.raises(ExceptionGroup) as caught:
            await owner.shutdown()
        assert _leaves(caught.value) == [error]
    finally:
        release.set()
        wait_entered.cancel()
        with suppress(asyncio.CancelledError):
            await wait_entered
        if stopped is not None:
            with suppress(BaseExceptionGroup):
                await stopped
        if owner.unfinished_watches:
            await asyncio.wait(owner.unfinished_watches, timeout=1)
