"""Bounded SIGTERM exit for the labeler daemon — a real child process.

The daemon must exit within a small bound of SIGTERM even with a
default-executor job mid-flight: the hard exit skips the executor join that
stalls ``asyncio.run``'s close (task #4224 — see ``daemon_shutdown_test_support``
for the harness and the daemon's ``_hard_exit`` for the mechanism).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ops.agent_pause import PAUSE_TIMEOUT_SECONDS
from tests.services.daemon_shutdown_test_support import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM path; Windows stops route through the private console",
)


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(tmp_path, module="services.labeler.daemon", label="labeler")
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[labeler] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
