"""Cross-domain stop budget for the backup scheduler's wedged executor."""

import sys
from pathlib import Path

from base.host.tests.daemon_shutdown import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(
        tmp_path,
        command=[sys.executable, "-m", "services.backup.scheduler.tests.child_shutdown"],
        label="pg-backup",
    )
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[pg-backup] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
