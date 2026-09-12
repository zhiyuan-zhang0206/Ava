"""services.ava_root.supervisor: the supervise loop over real subprocesses.

Every test drives real child processes through the supervisor: bring-up,
subtree verbs, restart policies with backoff, the start-new-before-stop-old
replacement, the reap path, and the chain assertions (a unit's parent must
stay this process — no double-fork, no detaching).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.supervisor import Supervisor, SupervisorConfig


def _python(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


_SLEEP_FOREVER = _python("import time; time.sleep(60)")


def _exit_now(code: int) -> list[str]:
    return _python(f"import sys; sys.exit({code})")


def _unit(
    unit_id: str,
    cmd: list[str],
    *,
    attach: str = "root",
    restart: str = "always",
) -> UnitManifest:
    return UnitManifest(id=unit_id, exec=tuple(cmd), restart=RestartPolicy(restart), attach=attach)


def _make(units: list[UnitManifest], log_dir: Path, **cfg: float) -> Supervisor:
    return Supervisor(UnitRegistry(units), log_dir=log_dir, config=SupervisorConfig(**cfg))


StartFactory = Callable[..., Awaitable[Supervisor]]


@pytest.fixture
async def started(short_tmp: Path) -> AsyncIterator[StartFactory]:
    """Factory: start a supervisor; every tree it made is torn down at test end."""
    created: list[Supervisor] = []

    async def factory(units: list[UnitManifest], **cfg: float) -> Supervisor:
        supervisor = _make(units, short_tmp / "logs", **cfg)
        created.append(supervisor)
        await supervisor.start()
        return supervisor

    yield factory
    for supervisor in created:
        await supervisor.shutdown()


async def _unit_status(supervisor: Supervisor, unit_id: str) -> dict[str, object]:
    status = await supervisor.status()
    units = cast("list[dict[str, object]]", status["units"])
    for entry in units:
        if entry["id"] == unit_id:
            return entry
    raise AssertionError(f"unit {unit_id!r} missing from status")


async def _wait_until(predicate: Callable[[], Awaitable[bool]], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met before the deadline")


async def _assert_dead(pid: int, *, timeout: float = 3.0) -> None:
    """The pid is gone from the process table (killed and reaped)."""

    async def gone() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    await _wait_until(gone, timeout=timeout)


def _ppid(pid: int) -> int:
    out = subprocess.run(  # noqa: S603 — fixed system tool, literal argv, no shell
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


async def test_start_brings_up_tree_with_intact_parent_chain(
    started: StartFactory,
) -> None:
    supervisor = await started(
        [
            _unit("parent", _SLEEP_FOREVER),
            _unit("child", _SLEEP_FOREVER, attach="parent"),
        ]
    )
    parent = await _unit_status(supervisor, "parent")
    child = await _unit_status(supervisor, "child")
    assert parent["state"] == "running"
    assert child["state"] == "running"
    parent_pid = cast(int, parent["pid"])
    child_pid = cast(int, child["pid"])
    # I2: both generations are plain children of this process — no double-fork,
    # no new session; the chain is not truncated anywhere below the root.
    assert _ppid(parent_pid) == os.getpid()
    assert _ppid(child_pid) == os.getpid()
    # setsid would leave the ppid intact but detach the unit from this
    # process's session — also forbidden by I2, so pin it explicitly.
    assert os.getsid(parent_pid) == os.getsid(0)
    assert os.getsid(child_pid) == os.getsid(0)
    # Unit output is captured under the run directory.
    assert (Path(supervisor._log_dir) / "parent.log").exists()


async def test_up_is_idempotent(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    before = await _unit_status(supervisor, "svc")
    response = await supervisor.up("svc")
    units = cast("list[dict[str, object]]", response["units"])
    assert [u["action"] for u in units] == ["already-running"]
    after = await _unit_status(supervisor, "svc")
    assert after["pid"] == before["pid"]


async def test_down_stops_subtree_children_first(started: StartFactory) -> None:
    supervisor = await started(
        [
            _unit("parent", _SLEEP_FOREVER),
            _unit("child", _SLEEP_FOREVER, attach="parent"),
        ]
    )
    child_pid = cast(int, (await _unit_status(supervisor, "child"))["pid"])
    response = await supervisor.down("parent")
    units = cast("list[dict[str, object]]", response["units"])
    # Children before parents — the reverse of the startup order.
    assert [u["id"] for u in units] == ["child", "parent"]
    assert [u["action"] for u in units] == ["stopped", "stopped"]
    for entry in units:
        assert entry["state"] == "stopped"
        assert entry["pid"] is None
    await _assert_dead(child_pid)
    assert (await _unit_status(supervisor, "parent"))["state"] == "stopped"


async def test_down_keeps_units_stopped_until_up(started: StartFactory) -> None:
    """An explicit down holds even for restart=always — no resurrection."""
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)], backoff_base_s=0.05)
    await supervisor.down("svc")
    await asyncio.sleep(0.3)
    assert (await _unit_status(supervisor, "svc"))["state"] == "stopped"
    response = await supervisor.up("svc")
    units = cast("list[dict[str, object]]", response["units"])
    assert [u["action"] for u in units] == ["started"]


async def test_restart_always_restarts_after_crash(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _exit_now(1), restart="always")], backoff_base_s=0.02)
    first = cast(int, (await _unit_status(supervisor, "svc"))["pid"] or 0)

    async def restarted() -> bool:
        entry = await _unit_status(supervisor, "svc")
        return cast(int, entry["restart_count"]) >= 1

    await _wait_until(restarted)
    entry = await _unit_status(supervisor, "svc")
    assert entry["state"] == "running"
    assert entry["pid"] != first


async def test_restart_never_holds_stopped(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _exit_now(3), restart="never")], backoff_base_s=0.02)

    async def stopped() -> bool:
        return (await _unit_status(supervisor, "svc"))["state"] == "stopped"

    await _wait_until(stopped)
    await asyncio.sleep(0.15)
    entry = await _unit_status(supervisor, "svc")
    assert entry["restart_count"] == 0
    assert entry["last_exit"] == "exit 3"


async def test_restart_on_failure_clean_exit_holds(started: StartFactory) -> None:
    supervisor = await started(
        [_unit("svc", _exit_now(0), restart="on-failure")], backoff_base_s=0.02
    )

    async def stopped() -> bool:
        return (await _unit_status(supervisor, "svc"))["state"] == "stopped"

    await _wait_until(stopped)
    await asyncio.sleep(0.15)
    assert (await _unit_status(supervisor, "svc"))["restart_count"] == 0


async def test_restart_on_failure_nonzero_restarts(started: StartFactory) -> None:
    supervisor = await started(
        [_unit("svc", _exit_now(2), restart="on-failure")], backoff_base_s=0.02
    )

    async def restarted() -> bool:
        entry = await _unit_status(supervisor, "svc")
        return cast(int, entry["restart_count"]) >= 1

    await _wait_until(restarted)


async def test_backoff_schedule_grows_and_caps(started: StartFactory) -> None:
    supervisor = await started(
        [_unit("svc", _SLEEP_FOREVER)], backoff_base_s=0.05, backoff_max_s=0.2
    )
    runtime = supervisor._units["svc"]
    delays: list[float] = []
    for _ in range(4):
        before = time.monotonic()
        supervisor._schedule_restart(runtime)
        backoff_until = runtime.backoff_until
        assert backoff_until is not None
        delays.append(backoff_until - before)
        task = runtime.restart_task
        assert task is not None
        task.cancel()
    assert delays == pytest.approx([0.05, 0.1, 0.2, 0.2], abs=0.03)


async def test_manual_restart_replaces_generation(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    old_pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])
    response = await supervisor.restart("svc")
    units = cast("list[dict[str, object]]", response["units"])
    assert [u["action"] for u in units] == ["replaced"]
    entry = await _unit_status(supervisor, "svc")
    assert entry["state"] == "running"
    assert entry["restart_count"] == 1
    new_pid = cast(int, entry["pid"])
    assert new_pid != old_pid
    await _assert_dead(old_pid)


async def test_restart_starts_a_stopped_unit(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    await supervisor.down("svc")
    response = await supervisor.restart("svc")
    units = cast("list[dict[str, object]]", response["units"])
    assert [u["action"] for u in units] == ["started"]
    entry = await _unit_status(supervisor, "svc")
    assert entry["state"] == "running"
    assert entry["restart_count"] == 0


async def test_restart_rolls_the_whole_subtree(started: StartFactory) -> None:
    supervisor = await started(
        [
            _unit("parent", _SLEEP_FOREVER),
            _unit("child", _SLEEP_FOREVER, attach="parent"),
        ]
    )
    before = {
        "parent": cast(int, (await _unit_status(supervisor, "parent"))["pid"]),
        "child": cast(int, (await _unit_status(supervisor, "child"))["pid"]),
    }
    await supervisor.restart("parent")
    for member, old_pid in before.items():
        entry = await _unit_status(supervisor, member)
        assert entry["state"] == "running"
        assert entry["pid"] != old_pid
        await _assert_dead(old_pid)


async def test_failed_spawn_keeps_old_generation(started: StartFactory, short_tmp: Path) -> None:
    """start-new-before-stop-old: a failed new start must not take the old
    instance down."""
    script = short_tmp / "unit.sh"
    script.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    supervisor = await started([_unit("svc", [str(script)], restart="never")])
    old_pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])
    # Drop the execute bit rather than unlinking: removing the file races the
    # shell's read of its own script (a shell that loses that race exits, and
    # then the old generation is legitimately gone). chmod cannot disturb the
    # running process, and the next execve gets EACCES.
    script.chmod(0o644)

    response = await supervisor.restart("svc")
    units = cast("list[dict[str, object]]", response["units"])
    assert [u["action"] for u in units] == ["failed"]
    assert "spawn failed" in str(units[0]["error"])
    entry = await _unit_status(supervisor, "svc")
    assert entry["state"] == "running"
    assert entry["pid"] == old_pid  # the old generation keeps serving
    os.kill(old_pid, 0)  # still alive


async def test_failed_initial_spawn_records_error_and_retries(started: StartFactory) -> None:
    supervisor = await started(
        [_unit("svc", ["/nonexistent-ava-root-test-binary"], restart="always")],
        backoff_base_s=0.05,
        backoff_max_s=0.1,
    )

    async def retried() -> bool:
        entry = await _unit_status(supervisor, "svc")
        return cast(int, entry["failure_streak"]) >= 2

    await _wait_until(retried)
    entry = await _unit_status(supervisor, "svc")
    assert entry["state"] == "backoff"
    assert "spawn failed" in str(entry["last_error"])
    # A down stops the retry loop.
    await supervisor.down("svc")
    assert (await _unit_status(supervisor, "svc"))["state"] == "stopped"


async def test_stop_escalates_to_kill_after_timeout(started: StartFactory, short_tmp: Path) -> None:
    ready = short_tmp / "term-ignored.ready"
    cmd = _python(
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    supervisor = await started([_unit("svc", cmd)], stop_timeout_s=0.3)
    pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])

    async def ready_written() -> bool:
        return ready.exists()

    # Only stop once the child has actually installed its handler; TERM racing
    # interpreter startup would kill it before the ignore even exists.
    await _wait_until(ready_written)

    started_at = time.monotonic()
    await supervisor.down("svc")
    elapsed = time.monotonic() - started_at
    # The polite stop was given its grace period before the forceful kill.
    assert 0.25 <= elapsed < 5.0
    await _assert_dead(pid)


async def test_shutdown_stops_everything(started: StartFactory) -> None:
    supervisor = await started([_unit("alpha", _SLEEP_FOREVER), _unit("beta", _SLEEP_FOREVER)])
    pids = [cast(int, (await _unit_status(supervisor, name))["pid"]) for name in ("alpha", "beta")]
    await supervisor.shutdown()
    for pid in pids:
        await _assert_dead(pid)
    status = await supervisor.status()
    root = cast("dict[str, object]", status["root"])
    assert root["running"] is False


async def test_exited_process_is_reaped_not_zombie(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _exit_now(0), restart="never")], backoff_base_s=0.02)
    pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])

    async def stopped() -> bool:
        return (await _unit_status(supervisor, "svc"))["state"] == "stopped"

    await _wait_until(stopped)
    # A zombie would still answer signal 0; ProcessLookupError proves the child
    # was waited on (reaped) rather than left dangling.
    await _assert_dead(pid)


async def test_concurrent_commands_settle_consistently(started: StartFactory) -> None:
    supervisor = await started([_unit("alpha", _SLEEP_FOREVER)])
    results = await asyncio.gather(
        supervisor.up("alpha"),
        supervisor.up("alpha"),
        supervisor.status(),
        supervisor.down("alpha"),
        supervisor.up("alpha"),
        supervisor.status(),
        return_exceptions=True,
    )
    assert not any(isinstance(r, BaseException) for r in results)
    entry = await _unit_status(supervisor, "alpha")
    assert entry["state"] in {"running", "stopped"}
    if entry["state"] == "running":
        assert entry["pid"] is not None


async def test_dispatch_translates_business_errors(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    response = await supervisor.dispatch({"verb": "up", "name": "ghost"})
    assert response["ok"] is False
    assert response.get("code") == "unknown_unit"
    assert "ghost" in str(response.get("error"))

    response = await supervisor.dispatch({"verb": "upgrade"})
    assert response["ok"] is False
    assert response.get("code") == "not_implemented"

    response = await supervisor.dispatch({"verb": "status"})
    assert response["ok"] is True
    result = cast("dict[str, object]", response.get("result"))
    root = cast("dict[str, object]", result["root"])
    assert root["pid"] == os.getpid()
