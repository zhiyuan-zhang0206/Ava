"""A failed background task cannot skip turn drain or ownership release.

The sweep's bounded-exit regression (task #4224) rides the shared child-process
harness: production ``main()`` must exit within a small bound of SIGTERM even
with a default-executor job mid-flight.
"""

import asyncio
import json
import signal
import subprocess
import sys
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.agent_pause import PAUSE_TIMEOUT_SECONDS
from tests.services.daemon_shutdown_test_support import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)


def _exercise_shutdown(failure: str) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    from agent import _process_boot, graph
    from services.agent_host import daemon

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
        if failure == "exception":
            # Let the failed task finish before entering the cleanup path.
            await asyncio.sleep(0.01)
            return
        await asyncio.Event().wait()

    original_spawn = daemon._spawn_background_tasks

    def spawn(pool: object) -> dict[str, asyncio.Task[object]]:
        if failure == "plugin":
            return original_spawn(pool)  # type: ignore[arg-type] -- pools are test doubles
        return {
            "failed": asyncio.create_task(fail()),
            "sibling": asyncio.create_task(background()),
        }

    host = MagicMock(aclose=partial(record, "owner_released"))
    scheduler = MagicMock(aclose=partial(record, "turns_drained"))
    workload, control = MagicMock(), MagicMock()

    async def close_pools(*_args: object) -> None:
        await record("pools_closed")

    async def close_health(*_args: object) -> None:
        await record("health_closed")

    def remove_pidfile(*_args: object) -> None:
        events.append("pidfile_removed")

    with (
        patch.multiple(
            _process_boot,
            init_process_scope=MagicMock(),
            land_cluster_extensions=MagicMock(),
            load_process_extensions=MagicMock(),
        ),
        patch.object(graph, "build_graph", return_value=MagicMock()),
        patch("services.agent_host.impersonation_events.reconcile_forever", background),
        patch.multiple(
            daemon,
            _is_running=MagicMock(return_value=False),
            acquire_pidfile=MagicMock(return_value=True),
            remove_pidfile=remove_pidfile,
            build_shared_pool=MagicMock(return_value=workload),
            build_control_pool=MagicMock(return_value=control),
            _open_host_pools=AsyncMock(),
            _close_host_pools=close_pools,
            _build_checkpointer=AsyncMock(),
            AgentHost=MagicMock(return_value=host),
            TurnScheduler=MagicMock(return_value=scheduler),
            _beat_forever=beat,
            settle_stranded_reaps_async=AsyncMock(return_value=[]),
            settle_stale_running_rows=AsyncMock(return_value=[]),
            start_health_server=AsyncMock(return_value=object()),
            stop_health_server=close_health,
            _spawn_background_tasks=spawn,
            _plugins_fingerprint=MagicMock(side_effect=["before", "after"]),
            _PLUGINS_POLL_INTERVAL_S=0.01,
            _page_reconcile_forever=background,
            _rotate_stdout_log_forever=background,
            _schedule_watcher_recovery=AsyncMock(),
            InboundWakeDispatcher=MagicMock(return_value=MagicMock(run=dispatch)),
        ),
    ):
        daemon.install_graceful_shutdown("agent_host_test")
        try:
            asyncio.run(daemon.run())
        except (KeyboardInterrupt, ValueError, asyncio.CancelledError) as exc:
            events.append(type(exc).__name__)
    print(json.dumps(events))  # noqa: T201 -- child result protocol


@pytest.mark.parametrize(
    "failure,exception",
    [("plugin", "KeyboardInterrupt"), ("signal", "KeyboardInterrupt"), ("exception", "ValueError")],
)
def test_failed_background_still_drains_and_releases(failure: str, exception: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed test helper in this checkout
        [
            sys.executable,
            "-c",
            "from tests.services.test_agent_host_shutdown import _exercise_shutdown; "
            f"_exercise_shutdown({failure!r})",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = json.loads(result.stdout.splitlines()[-1])
    assert events.count("background_joined") == (3 if failure == "plugin" else 1)
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
        event for event in events if event not in {"background_joined", "beat_stopped"}
    ] == ordered
    assert events.index("background_joined") < events.index("turns_drained")
    assert events.index("beat_stopped") < events.index("owner_released")
    if failure == "exception":
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
    child = spawn_child(tmp_path, module="services.agent_host.daemon", label="agent-host")
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[agent-host] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
