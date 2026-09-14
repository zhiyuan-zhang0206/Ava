"""services.ava_root.supervisor: the supervise loop over real subprocesses.

Every test drives real child processes through the supervisor: bring-up,
subtree verbs, restart policies with backoff, the start-new-before-stop-old
replacement, the reap path, and the chain assertions (a unit's parent must
stay this process — no double-fork, no detaching).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from services.ava_root.handoff import HandoffFile, HandoffUnit
from services.ava_root.manifest import (
    DesiredState,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
)
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


def _make(units: list[UnitManifest], run_dir: Path, **cfg: float) -> Supervisor:
    return Supervisor(UnitRegistry(units), run_dir=run_dir, config=SupervisorConfig(**cfg))


StartFactory = Callable[..., Awaitable[Supervisor]]


@pytest.fixture
async def started(short_tmp: Path) -> AsyncIterator[StartFactory]:
    """Factory: start a supervisor; every tree it made is torn down at test end."""
    created: list[Supervisor] = []

    async def factory(units: list[UnitManifest], **cfg: float) -> Supervisor:
        supervisor = _make(units, short_tmp, **cfg)
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
    assert (Path(supervisor._log_dir) / "parent" / "output.log").exists()


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


async def test_backoff_schedule_clamps_a_huge_failure_streak(started: StartFactory) -> None:
    """Regression (the wiring gate): an unclamped `2**streak` overflows the
    int-to-float conversion from streak=1024 on, raising before the `min()`
    cap could apply."""
    supervisor = await started(
        [_unit("svc", _SLEEP_FOREVER)], backoff_base_s=0.05, backoff_max_s=0.2
    )
    runtime = supervisor._units["svc"]
    runtime.failure_streak = 10_000
    before = time.monotonic()
    supervisor._schedule_restart(runtime)
    backoff_until = runtime.backoff_until
    assert backoff_until is not None
    assert backoff_until - before == pytest.approx(0.2, abs=0.03)
    task = runtime.restart_task
    assert task is not None
    task.cancel()


async def test_watch_survives_a_huge_streak_and_the_unit_restarts(
    started: StartFactory,
) -> None:
    """Regression: the overflow used to kill the watch task before
    `generation.exited` was set, so the unit silently stopped restarting while
    its status kept claiming running."""
    exits_soon = _python("import sys, time; time.sleep(0.3); sys.exit(1)")
    supervisor = await started(
        [_unit("svc", exits_soon, restart="always")],
        backoff_base_s=0.05,
        backoff_max_s=0.1,
    )
    runtime = supervisor._units["svc"]
    watch_task = runtime.watch_task
    runtime.failure_streak = 5000

    async def restarted() -> bool:
        entry = await _unit_status(supervisor, "svc")
        return cast(int, entry["restart_count"]) >= 1

    await _wait_until(restarted)
    assert watch_task is not None
    assert watch_task.done()
    assert watch_task.exception() is None


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

    response = await supervisor.dispatch({"verb": "status"})
    assert response["ok"] is True
    result = cast("dict[str, object]", response.get("result"))
    root = cast("dict[str, object]", result["root"])
    assert root["pid"] == os.getpid()


# -- revival_deferral: the seam that keeps a second reviver from fighting ------


async def test_revival_deferral_none_for_a_healthy_unit(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    assert supervisor.revival_deferral("svc") is None


async def test_revival_deferral_held_down_after_operator_stop(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    await supervisor.down("svc")
    assert supervisor.revival_deferral("svc") == "held down"


async def test_revival_deferral_already_scheduled_during_retry(started: StartFactory) -> None:
    supervisor = await started(
        [_unit("svc", _SLEEP_FOREVER)], backoff_base_s=60.0, backoff_max_s=120.0
    )
    pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])
    os.kill(pid, signal.SIGKILL)

    async def scheduled() -> bool:
        return supervisor.revival_deferral("svc") == "already scheduled"

    await _wait_until(scheduled)


async def test_revival_deferral_policy_never(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER, restart="never")])
    assert supervisor.revival_deferral("svc") == "policy never"


async def test_revival_deferral_held_down_wins_over_never(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER, restart="never")])
    await supervisor.down("svc")
    assert supervisor.revival_deferral("svc") == "held down"


async def test_revival_deferral_unknown_unit(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    with pytest.raises(UnknownUnitError):
        supervisor.revival_deferral("ghost")


# -- status attach seams + tree_view (W1.2b) -----------------------------------


async def test_status_embeds_attached_surfaces_only(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    status = await supervisor.status()
    assert "health" not in status
    assert "metrics" not in status

    class _Health:
        def health_snapshot(self) -> dict[str, object]:
            return {"svc": {"breaker_open": False}}

    class _Metrics:
        def metrics_snapshot(self) -> dict[str, object]:
            return {"chain": {"broken": False}}

    supervisor.attach_health(_Health())
    supervisor.attach_metrics(_Metrics())
    status = await supervisor.status()
    assert status["health"] == {"svc": {"breaker_open": False}}
    assert status["metrics"] == {"chain": {"broken": False}}
    assert "units" in status and "root" in status and "restarts_total" in status


async def test_tree_view_reports_the_raw_recorded_pid(started: StartFactory) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    view = supervisor.tree_view()
    assert view["root_pid"] == os.getpid()
    units = cast("list[dict[str, object]]", view["units"])
    assert units[0]["id"] == "svc" and units[0]["state"] == "running"
    pid = cast(int, units[0]["pid"])

    # Disarm the watch and kill: status() masks the dead generation's pid to
    # None, but tree_view keeps carrying the recorded pid the self-check judges.
    runtime = supervisor._units["svc"]
    assert runtime.watch_task is not None
    runtime.watch_task.cancel()
    os.kill(pid, signal.SIGKILL)
    await asyncio.sleep(0.15)
    assert (await _unit_status(supervisor, "svc"))["pid"] is None
    units = cast("list[dict[str, object]]", supervisor.tree_view()["units"])
    assert units[0]["pid"] == pid

    await supervisor.up("svc")  # leave a healthy generation for teardown


# -- the upgrade handoff: snapshot, fail-fast write, steady-state warnings -------


def _kill_quietly(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _wait_zombie(pid: int, *, timeout: float = 5.0) -> None:
    """Wait until a killed child is an unreaped zombie (its exit is pending)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(  # noqa: S603 — fixed system tool, literal argv, no shell
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stdout.strip().startswith("Z"):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not become an unreaped zombie")


async def test_upgrade_snapshots_the_tree_and_answers_accepted(
    started: StartFactory, short_tmp: Path
) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])
    unit_pid = cast(int, (await _unit_status(supervisor, "svc"))["pid"])

    response = await supervisor.dispatch({"verb": "upgrade"})
    assert response["ok"] is True
    result = cast("dict[str, object]", response.get("result"))
    assert result["accepted"] is True
    assert result["root_pid"] == os.getpid()
    assert result["units_total"] == 1 and result["units_running"] == 1

    raw = cast("dict[str, object]", json.loads((short_tmp / "handoff.json").read_text("utf-8")))
    assert raw["writer_pid"] == os.getpid()
    (carried,) = cast("list[dict[str, object]]", raw["units"])
    assert carried["id"] == "svc" and carried["pid"] == unit_pid
    assert carried["desired"] == "running"
    assert cast(float, carried["started_at"]) > 0.0
    assert not (short_tmp / "handoff.json.tmp").exists()

    assert supervisor.take_pending_upgrade() is True
    assert supervisor.take_pending_upgrade() is False


async def test_upgrade_write_failure_is_an_error_and_leaves_the_tree(
    started: StartFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = await started([_unit("svc", _SLEEP_FOREVER)])

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full (test)")

    monkeypatch.setattr("services.ava_root.supervisor.write_handoff", fail_write)
    response = await supervisor.dispatch({"verb": "upgrade"})
    assert response["ok"] is False
    assert response.get("code") == "internal"
    assert supervisor.take_pending_upgrade() is False
    assert (await _unit_status(supervisor, "svc"))["state"] == "running"


async def test_upgrade_warns_when_a_unit_is_not_in_steady_state(
    started: StartFactory, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor = await started([_unit("svc", _exit_now(1))], backoff_base_s=100.0)

    async def in_backoff() -> bool:
        return (await _unit_status(supervisor, "svc"))["state"] == "backoff"

    await _wait_until(in_backoff)
    with caplog.at_level("WARNING", logger="services.ava_root.supervisor"):
        response = await supervisor.dispatch({"verb": "upgrade"})
    assert response["ok"] is True
    assert any("not in steady state" in record.getMessage() for record in caplog.records)


# -- taking over an inherited tree: attach instead of respawn --------------------


async def test_start_from_handoff_attaches_live_children_without_spawning(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = subprocess.Popen(_SLEEP_FOREVER, cwd=short_tmp)  # noqa: S603 — test's own child
    second = subprocess.Popen(_SLEEP_FOREVER, cwd=short_tmp)  # noqa: S603 — test's own child
    handoff = HandoffFile.stamp(
        os.getpid(),
        (
            HandoffUnit(
                "svc-a",
                DesiredState.RUNNING,
                pid=first.pid,
                pgid=os.getpgid(first.pid),
                started_at=time.monotonic(),
            ),
            HandoffUnit(
                "svc-b",
                DesiredState.RUNNING,
                pid=second.pid,
                pgid=os.getpgid(second.pid),
                started_at=time.monotonic(),
            ),
        ),
    )
    spawns: list[str] = []
    original_spawn = Supervisor._spawn

    async def spy(self: Supervisor, runtime: Any) -> None:
        spawns.append(runtime.manifest.id)
        await original_spawn(self, runtime)

    monkeypatch.setattr(Supervisor, "_spawn", spy)
    supervisor = _make([_unit("svc-a", _SLEEP_FOREVER), _unit("svc-b", _SLEEP_FOREVER)], short_tmp)
    try:
        await supervisor.start(handoff=handoff)
        assert spawns == []  # both attached in place
        assert (await _unit_status(supervisor, "svc-a"))["pid"] == first.pid
        assert (await _unit_status(supervisor, "svc-b"))["pid"] == second.pid

        # The reaper is re-attached: a child's exit still drives the policy.
        os.kill(first.pid, signal.SIGKILL)

        async def svc_a_replaced() -> bool:
            entry = await _unit_status(supervisor, "svc-a")
            return entry["pid"] is not None and entry["pid"] != first.pid

        await _wait_until(svc_a_replaced)
        assert spawns == ["svc-a"]
    finally:
        await supervisor.shutdown()
        _kill_quietly(first.pid)
        _kill_quietly(second.pid)


async def test_start_from_handoff_starts_a_unit_whose_child_already_exited(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = subprocess.Popen(_SLEEP_FOREVER, cwd=short_tmp)  # noqa: S603 — test's own child
    dead.kill()
    _wait_zombie(dead.pid)  # keep it unreaped: the handoff probe must reap it
    handoff = HandoffFile.stamp(
        os.getpid(),
        (
            HandoffUnit(
                "svc",
                DesiredState.RUNNING,
                pid=dead.pid,
                pgid=None,
                started_at=time.monotonic(),
            ),
        ),
    )
    spawns: list[str] = []
    original_spawn = Supervisor._spawn

    async def spy(self: Supervisor, runtime: Any) -> None:
        spawns.append(runtime.manifest.id)
        await original_spawn(self, runtime)

    monkeypatch.setattr(Supervisor, "_spawn", spy)
    supervisor = _make([_unit("svc", _SLEEP_FOREVER)], short_tmp)
    try:
        await supervisor.start(handoff=handoff)
        assert spawns == ["svc"]  # gone at takeover: the start path
        entry = await _unit_status(supervisor, "svc")
        assert entry["state"] == "running"
        assert entry["pid"] != dead.pid
        assert entry["last_exit"] == "signal 9"
    finally:
        await supervisor.shutdown()
        _kill_quietly(dead.pid)


async def test_start_from_handoff_keeps_a_stopped_unit_stopped(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = subprocess.Popen(_SLEEP_FOREVER, cwd=short_tmp)  # noqa: S603 — test's own child
    handoff = HandoffFile.stamp(
        os.getpid(),
        (
            HandoffUnit("held", DesiredState.STOPPED),
            HandoffUnit(
                "svc",
                DesiredState.RUNNING,
                pid=alive.pid,
                pgid=os.getpgid(alive.pid),
                started_at=time.monotonic(),
            ),
        ),
    )
    spawns: list[str] = []
    original_spawn = Supervisor._spawn

    async def spy(self: Supervisor, runtime: Any) -> None:
        spawns.append(runtime.manifest.id)
        await original_spawn(self, runtime)

    monkeypatch.setattr(Supervisor, "_spawn", spy)
    supervisor = _make([_unit("held", _SLEEP_FOREVER), _unit("svc", _SLEEP_FOREVER)], short_tmp)
    try:
        await supervisor.start(handoff=handoff)
        assert spawns == []  # operator intent survives: stopped stays stopped
        assert (await _unit_status(supervisor, "held"))["state"] == "stopped"
        assert (await _unit_status(supervisor, "svc"))["pid"] == alive.pid
    finally:
        await supervisor.shutdown()
        _kill_quietly(alive.pid)


async def test_start_from_handoff_warns_about_unknown_carried_units(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handoff = HandoffFile.stamp(
        os.getpid(),
        (
            HandoffUnit("svc", DesiredState.RUNNING),
            HandoffUnit("ghost", DesiredState.RUNNING),
        ),
    )
    supervisor = _make([_unit("svc", _SLEEP_FOREVER)], short_tmp)
    try:
        with caplog.at_level("WARNING", logger="services.ava_root.supervisor"):
            await supervisor.start(handoff=handoff)
        assert (await _unit_status(supervisor, "svc"))["state"] == "running"
        assert any("not in this registry" in record.getMessage() for record in caplog.records)
    finally:
        await supervisor.shutdown()
