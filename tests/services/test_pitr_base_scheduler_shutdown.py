"""Real child-process regression: the base-candidate daemon exits within a small
bound of SIGTERM even with a default-executor job mid-flight and the OTLP
endpoint in a dead window.

2026-09-18 (task #3940): the wsl unit's smooth stop SIGTERM'd this daemon while
its retention refresh — a routine multi-minute ``asyncio.to_thread`` scan
(#3345) — was in flight. ``asyncio.run``'s close awaits
``shutdown_default_executor``, capped by CPython at ``THREAD_JOIN_TIMEOUT``
(300 s) — the stop flow's entire budget (`PAUSE_TIMEOUT_SECONDS`). The exit
lost that race by ~2 s (stop judged incomplete and the unit stood half-stopped
for ~6.5 min until the watchdog respawn), and interpreter teardown then joins a
still-running worker with no bound at all. The daemon now hard-exits after an
explicit cancellation drain and never joins the executor.

The child runs the production ``main()`` — signal wiring included — through the
shared harness (``daemon_shutdown_test_support``; task #4239 folded this test's
inline apparatus into it): ``run()`` is swapped for a wedge-shaped surrogate,
and the parent asserts the bounded exit and the cleanup marker, with its own
kill deadline so the old unbounded shape fails the test instead of hanging the
suite. The dead OTLP window rides the operator's real channels (a throwaway
``$AVA_HOME`` and the process environment) and is driven by
``_probe_dead_otlp_window``, the ``pre_ready`` probe the child runs once the
wedge is armed: the exporter's failed bring-up must be observed before the
signal, or the test would not reproduce the incident's combination.
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


def _probe_dead_otlp_window() -> None:
    """Child-side pre-ready probe: drive the OTLP bring-up synchronously so its failure is observable.

    Runs in the child once the wedge is armed (``spawn_child(pre_ready=...)``):
    the exporter's failed bring-up must land before the signal, or the
    incident's combination — dead OTLP window with an executor job in flight at
    SIGTERM — is not reproduced.
    """
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "pitr_remote_inventory",
        attributes={
            "backend": "test",
            "object_count": 0,
            "bytes": 0,
            "logical_object_count": 0,
            "logical_bytes": 0,
        },
    )
    telemetry.flush()


def test_sigterm_bounded_exit_with_wedged_executor_and_dead_otlp(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged executor job and a dead OTLP window stand."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = spawn_child(
        tmp_path,
        module="services.pitr.base_scheduler_daemon",
        label="pitr",
        pre_ready=_probe_dead_otlp_window,
    )
    try:
        log = child.log_tail()
        assert "not answering" in log, f"the dead OTLP window was never observed:\n{log}"
        child.terminate()
        child.wait_bounded_exit(what="wedged executor + dead OTLP window")
        assert "[pitr-base-candidate] interrupted, shutting down" in child.log_tail(), (
            child.log_tail()
        )
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
