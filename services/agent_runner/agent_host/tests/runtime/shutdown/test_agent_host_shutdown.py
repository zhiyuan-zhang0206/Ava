"""A failed background loop exits the host, and cannot skip turn drain or ownership release.

The loops share one `TaskGroup` with the wake dispatcher: a loop that raises
cancels the dispatcher and its siblings, and `run` raises so `ava-root` restarts
the process after the turns drain.

The sweep's bounded-exit regression (task #4224) rides the shared child-process
harness: production ``main()`` must exit within a small bound of SIGTERM even
with a default-executor job mid-flight.
"""

import asyncio
import contextlib
import json
import signal
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.tests.daemon_shutdown import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)
from base.lm.catalog import ModelCatalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.composer_commands import CommandCatalog
from base.telemetry import process_name
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS
from services.agent_runner.agent_host.tests.host_policy import configured_policy


def _note_pidfile_removed(events: list[str], _path: object) -> None:
    events.append("pidfile_removed")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        members = cast("BaseExceptionGroup[BaseException]", exc).exceptions
        return f"ExceptionGroup[{','.join(type(e).__name__ for e in members)}]"
    return type(exc).__name__


async def _beat_for_shutdown(
    events: list[str], failure: str, *_args: object, **_kwargs: object
) -> None:
    try:
        events.append("beat_started")
        if failure in {"heartbeat", "heartbeat_boot"}:
            events.append("beat_failed")
            raise RuntimeError("heartbeat defect")
        await asyncio.Event().wait()
    finally:
        events.append("beat_stopped")


async def _dispatch_for_shutdown(events: list[str], failure: str) -> None:
    if failure == "heartbeat":
        for _ in range(4):
            await asyncio.sleep(0)
        events.append("dispatcher_continued")
        raise ValueError("dispatcher stopped independently")
    if failure == "dispatcher_returns":
        await asyncio.sleep(0.01)  # let the sibling loop start before the return
        return
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        events.append("dispatcher_cancelled")
        raise


def _shutdown_config() -> ConfigBoot:
    config = ConfigBoot()
    config.boot()
    return config


def _assert_boot_primary(exc: BaseException, failure: str) -> None:
    if failure == "heartbeat_boot":
        assert isinstance(exc, asyncio.CancelledError)
        assert any("heartbeat defect" in note for note in exc.__notes__)


def exercise_shutdown(failure: str, home: Path) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    database_gate = ProcessDbGate(version=version.get, process=process_name(), exempt=False)
    _run_shutdown(failure, database_gate=database_gate, home=home)


def _run_shutdown(failure: str, *, database_gate: ProcessDbGate, home: Path) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    from agent import graph, process_boot

    from .... import daemon

    events: list[str] = []

    async def record(name: str, **_kwargs: object) -> None:
        await asyncio.sleep(0)
        events.append(name)

    beat = partial(_beat_for_shutdown, events, failure)

    async def background(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await record("background_joined")

    async def fail() -> None:
        await asyncio.sleep(0)
        if failure == "signal":
            signal.raise_signal(signal.SIGTERM)
        raise ValueError("background failed")

    dispatch = partial(_dispatch_for_shutdown, events, failure)

    async def settle(*_args: object, **_kwargs: object) -> list[int]:
        if failure == "heartbeat_boot":
            for _ in range(4):
                await asyncio.sleep(0)
            events.append("boot_continued")
            raise ValueError("boot settlement stopped independently")
        await asyncio.sleep(0)
        assert "beat_started" in events
        events.append("boot_settled")
        return []

    original_loops = daemon.background_loops

    def loops(*args: Any, catalog: ModelCatalog, config: ConfigBoot) -> dict[str, Any]:
        if failure == "plugin":
            return original_loops(*args, catalog=catalog, config=config)
        failing = {} if failure in {"dispatcher_returns", "heartbeat"} else {"failed": fail()}
        return {**failing, "sibling": background()}

    host = MagicMock(aclose=partial(record, "owner_released"))
    scheduler = MagicMock(aclose=partial(record, "turns_drained"))
    workload, control = MagicMock(), MagicMock()

    async def close_pools(*_args: object) -> None:
        await record("pools_closed")

    async def close_health(*_args: object) -> None:
        await record("health_closed")

    def close_sampling(owner: SamplingPolicyOwner) -> bool:
        events.append("sampling_stopped")
        return owner.stop()

    with (
        patch.object(SamplingPolicyOwner, "close", close_sampling),
        patch.multiple(
            process_boot,
            init_process_scope=MagicMock(),
            land_cluster_extensions=MagicMock(),
        ),
        patch.object(graph, "build_graph", return_value=MagicMock()),
        patch.multiple(
            daemon,
            _is_running=MagicMock(return_value=False),
            acquire_pidfile=MagicMock(return_value=True),
            remove_pidfile=MagicMock(side_effect=partial(_note_pidfile_removed, events)),
            build_shared_pool=MagicMock(return_value=workload),
            build_control_pool=MagicMock(return_value=control),
            open_host_pools=AsyncMock(),
            close_host_pools=close_pools,
            build_checkpointer=AsyncMock(),
            AgentHost=MagicMock(return_value=host),
            TurnScheduler=MagicMock(return_value=scheduler),
            beat_forever=beat,
            settle_stale_running_rows=settle,
            start_health_server=AsyncMock(return_value=object()),
            stop_health_server=close_health,
            background_loops=loops,
            plugins_fingerprint=MagicMock(side_effect=["before", "after"]),
            PLUGINS_POLL_INTERVAL_S=0.01,
            rotate_stdout_log_forever=background,
            InboundWakeDispatcher=MagicMock(return_value=MagicMock(run=dispatch)),
        ),
    ):
        daemon.install_graceful_shutdown("agent_host_test")
        try:
            machine = MachineIdentity(
                name=lambda: "shutdown-test",
                role=lambda: frozenset({"agent-runner"}),
                host=lambda: "localhost",
                description=lambda: None,
            )
            asyncio.run(
                daemon.run(
                    config=_shutdown_config(),
                    database=lambda: Database.from_settings(gate=database_gate),
                    machine=machine,
                    home=home,
                    timers=SYSTEM_TIMERS,
                )
            )
        except (
            KeyboardInterrupt,
            asyncio.CancelledError,
            ExceptionGroup,
            RuntimeError,
            ValueError,
        ) as exc:
            _assert_boot_primary(exc, failure)
            events.append(_describe(exc))
    print(json.dumps(events))  # noqa: T201 -- child result protocol


def _assert_heartbeat_order(events: list[str], failure: str) -> None:
    assert events.index("beat_started") < events.index("boot_settled")
    assert events.index("beat_stopped") < events.index("owner_released")
    assert events.index("sampling_stopped") < events.index("pools_closed")
    if failure in {"exception", "dispatcher_returns"}:
        assert events.index("turns_drained") < events.index("beat_stopped")
        assert events.index("turns_drained") < events.index("sampling_stopped")
    if failure == "heartbeat":
        assert "dispatcher_continued" not in events
        assert events.index("beat_failed") < events.index("pools_closed")


def _shutdown_child(failure: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test helper in this checkout
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "agent_host" / "child_runtime_scenarios.py"),
            "shutdown",
            failure,
            str(home),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "failure,exception",
    [
        ("plugin", "KeyboardInterrupt"),
        ("signal", "KeyboardInterrupt"),
        ("exception", "ExceptionGroup[ValueError]"),
        ("dispatcher_returns", "ExceptionGroup[RuntimeError]"),
        ("heartbeat", "ExceptionGroup[RuntimeError]"),
    ],
)
def test_failed_background_still_drains_and_releases(
    failure: str, exception: str, unit_home: Path
) -> None:
    result = _shutdown_child(failure, unit_home)
    assert result.returncode == 0, result.stdout + result.stderr
    events = json.loads(result.stdout.splitlines()[-1])
    assert events.count("background_joined") == 1
    # A process interrupt lets asyncio cancel the heartbeat immediately. Both
    # restart and background-failure paths must still drain turns and release
    # ownership before closing their DB pools.
    ordered = [
        "turns_drained",
        "owner_released",
        "health_closed",
        "sampling_stopped",
        "pools_closed",
        "pidfile_removed",
        exception,
    ]
    assert [
        event
        for event in events
        if event
        not in {
            "background_joined",
            "beat_stopped",
            "dispatcher_cancelled",
            "beat_failed",
            "beat_started",
            "boot_settled",
            "dispatcher_continued",
        }
    ] == ordered
    assert events.index("background_joined") < events.index("turns_drained")
    _assert_heartbeat_order(events, failure)
    if failure == "exception":
        # The crashed loop took the dispatcher down with it, before any drain.
        assert events.index("dispatcher_cancelled") < events.index("turns_drained")
        assert events.index("turns_drained") < events.index("beat_stopped")


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(
        tmp_path,
        command=[
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.runtime.shutdown.child_shutdown",
        ],
        label="agent-host",
    )
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[agent-host] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_internal_sigterm_drains_async_cleanup_before_exit(
    tmp_path: Path, cleanup_failure: bool
) -> None:
    child = spawn_child(
        tmp_path,
        command=[
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.runtime.shutdown.child_shutdown",
        ],
        label="agent-host",
        internal_stop=True,
        cleanup_failure=cleanup_failure,
    )
    try:
        child.wait_bounded_exit(
            what="TaskGroup child requested SIGTERM with a wedged executor",
            expected_codes=(1,) if cleanup_failure else (0,),
        )
        markers = child.markers().splitlines()
        assert markers.index("cleanup-started") < markers.index("cleanup-ran")
        assert "[agent-host] interrupted, shutting down" in child.log_tail()
        if cleanup_failure:
            assert "async shutdown failed" in child.log_tail()
            assert "surrogate cleanup failure" in child.log_tail()
    finally:
        child.close()


class _UnreachablePool:
    """Every borrow waits, as a pool whose server is gone does until its timeout."""

    @contextlib.asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncGenerator[object]:
        del timeout
        await asyncio.Event().wait()
        yield object()


async def test_stop_releases_ownership_within_a_bound_when_postgres_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A/B/A run 6: with the pooler gone, the stop path's ownership release waited
    out the control pool's 30 s acquire timeout, so ava-root's 10 s TERM window
    expired first and root retained custody of the still-exiting agent-host."""
    from .... import host as host_mod

    monkeypatch.setattr(host_mod, "RELEASE_OWNER_TIMEOUT_S", 0.05)
    machine_identity.set(name="this-box")
    host = host_mod.AgentHost(
        policy=configured_policy(),
        pool=cast(Any, _UnreachablePool()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine=machine_identity,
        catalog=model_catalog,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        commands=CommandCatalog(skills=list),
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="ownership release"):
        await asyncio.wait_for(host.aclose(), timeout=2.0)
    assert time.monotonic() - started < 1.0


def test_failed_heartbeat_during_boot_still_closes_pools_and_pidfile(unit_home: Path) -> None:
    result = _shutdown_child("heartbeat_boot", unit_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [
        "beat_started",
        "beat_failed",
        "beat_stopped",
        "sampling_stopped",
        "pools_closed",
        "pidfile_removed",
        "CancelledError",
    ]
