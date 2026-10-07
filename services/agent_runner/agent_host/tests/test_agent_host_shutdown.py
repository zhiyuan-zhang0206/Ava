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

from base.db import Database
from base.events.live.bus import EventBus
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS
from tests.services.daemon_shutdown_test_support import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)


def _note_pidfile_removed(events: list[str], _path: object) -> None:
    events.append("pidfile_removed")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        members = cast("BaseExceptionGroup[BaseException]", exc).exceptions
        return f"ExceptionGroup[{','.join(type(e).__name__ for e in members)}]"
    return type(exc).__name__


def _exercise_shutdown(failure: str) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    from agent import graph, process_boot
    from services.agent_runner.agent_host import daemon

    events: list[str] = []

    async def record(name: str) -> None:
        await asyncio.sleep(0)
        events.append(name)

    async def beat(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("beat_stopped")

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

    async def dispatch() -> None:
        if failure == "dispatcher_returns":
            await asyncio.sleep(0.01)  # let the sibling loop start before the return
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("dispatcher_cancelled")
            raise

    original_loops = daemon._background_loops

    def loops(*args: Any) -> dict[str, Any]:
        if failure == "plugin":
            return original_loops(*args)
        failing = {} if failure == "dispatcher_returns" else {"failed": fail()}
        return {**failing, "sibling": background()}

    host = MagicMock(aclose=partial(record, "owner_released"))
    scheduler = MagicMock(aclose=partial(record, "turns_drained"))
    workload, control = MagicMock(), MagicMock()

    async def close_pools(*_args: object) -> None:
        await record("pools_closed")

    async def close_health(*_args: object) -> None:
        await record("health_closed")

    with (
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
            _open_host_pools=AsyncMock(),
            _close_host_pools=close_pools,
            _build_checkpointer=AsyncMock(),
            AgentHost=MagicMock(return_value=host),
            TurnScheduler=MagicMock(return_value=scheduler),
            _beat_forever=beat,
            settle_stale_running_rows=AsyncMock(return_value=[]),
            start_health_server=AsyncMock(return_value=object()),
            stop_health_server=close_health,
            _background_loops=loops,
            _plugins_fingerprint=MagicMock(side_effect=["before", "after"]),
            _PLUGINS_POLL_INTERVAL_S=0.01,
            _rotate_stdout_log_forever=background,
            InboundWakeDispatcher=MagicMock(return_value=MagicMock(run=dispatch)),
        ),
    ):
        daemon.install_graceful_shutdown("agent_host_test")
        try:
            asyncio.run(daemon.run())
        except (KeyboardInterrupt, asyncio.CancelledError, ExceptionGroup) as exc:
            events.append(_describe(exc))
    print(json.dumps(events))  # noqa: T201 -- child result protocol


@pytest.mark.parametrize(
    "failure,exception",
    [
        ("plugin", "KeyboardInterrupt"),
        ("signal", "KeyboardInterrupt"),
        ("exception", "ExceptionGroup[ValueError]"),
        ("dispatcher_returns", "ExceptionGroup[RuntimeError]"),
    ],
)
def test_failed_background_still_drains_and_releases(failure: str, exception: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed test helper in this checkout
        [
            sys.executable,
            "-c",
            "from services.agent_runner.agent_host.tests.test_agent_host_shutdown import _exercise_shutdown; "
            f"_exercise_shutdown({failure!r})",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
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
        "pools_closed",
        "pidfile_removed",
        exception,
    ]
    assert [
        event
        for event in events
        if event not in {"background_joined", "beat_stopped", "dispatcher_cancelled"}
    ] == ordered
    assert events.index("background_joined") < events.index("turns_drained")
    assert events.index("beat_stopped") < events.index("owner_released")
    if failure == "exception":
        # The crashed loop took the dispatcher down with it, before any drain.
        assert events.index("dispatcher_cancelled") < events.index("turns_drained")
        assert events.index("turns_drained") < events.index("beat_stopped")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM path; Windows stops route through the private console",
)
def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(
        tmp_path, module="services.agent_runner.agent_host.daemon", label="agent-host"
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


class _UnreachablePool:
    """Every borrow waits, as a pool whose server is gone does until its timeout."""

    @contextlib.asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncGenerator[object]:
        del timeout
        await asyncio.Event().wait()
        yield object()


async def test_stop_releases_ownership_within_a_bound_when_postgres_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A/B/A run 6: with the pooler gone, the stop path's ownership release waited
    out the control pool's 30 s acquire timeout, so ava-root's 10 s TERM window
    expired first and root retained custody of the still-exiting agent-host."""
    from services.agent_runner.agent_host import host as host_mod

    monkeypatch.setattr(host_mod, "_RELEASE_OWNER_TIMEOUT_S", 0.05)
    host = host_mod.AgentHost(
        pool=cast(Any, _UnreachablePool()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine="this-box",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="ownership release"):
        await asyncio.wait_for(host.aclose(), timeout=2.0)
    assert time.monotonic() - started < 1.0
