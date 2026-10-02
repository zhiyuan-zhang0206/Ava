"""The hosted daemon's background-loop wiring: which loops one TaskGroup owns."""

from __future__ import annotations

import services.agent_host.daemon as daemon_mod


def test_background_loops_are_the_plugin_watch_the_log_rotator_and_the_memory_guard() -> None:
    """The page scan of busy agents moved to the page-server service, so a
    page reconciler must not come back here (it would probe the same rows twice);
    the rotator keeps a traceback storm from filling the disk through the
    uncapped raw transcript (task #2356), and a regression that dropped it must
    turn a test red."""
    loops = daemon_mod._background_loops()
    try:
        assert set(loops) == {"plugins_watch", "stdout_log_rotate", "exec_memory_guard"}
    finally:
        for loop in loops.values():
            loop.close()
