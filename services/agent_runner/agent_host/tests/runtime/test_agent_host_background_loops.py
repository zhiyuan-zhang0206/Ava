"""The hosted daemon's background-loop wiring: which loops one TaskGroup owns."""

from __future__ import annotations

from unittest.mock import MagicMock

import services.agent_runner.agent_host.daemon as daemon_mod
from base.lm.catalog import ModelCatalog


def test_background_loops_are_the_plugin_watch_the_log_rotator_the_memory_guard_and_understanding(
    model_catalog: ModelCatalog,
) -> None:
    """The page scan of busy agents moved to the page-server service, so a
    page reconciler must not come back here (it would probe the same rows twice);
    the rotator keeps a traceback storm from filling the disk through the
    uncapped raw transcript (task #2356), and a regression that dropped it must
    turn a test red."""
    loops = daemon_mod._background_loops(
        MagicMock(),
        MagicMock(),
        catalog=model_catalog,
    )
    try:
        assert set(loops) == {
            "plugins_watch",
            "stdout_log_rotate",
            "exec_memory_guard",
            "understanding_chunks",
        }
    finally:
        for loop in loops.values():
            loop.close()
