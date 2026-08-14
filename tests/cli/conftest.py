"""Shared fixtures for the CLI tests.

The `ava cluster update` orchestration tests (`_run_gateway_orchestration` and friends)
all reach one seam that is not obvious from the code under test: the `finally`
unpauses the LOCAL host, and the real `ops.cluster.unpause_local_cluster` spawns
a live `services.restarter.daemon` in a session. Every one of these tests
stubbed the remote fan-out and left that leg real, so each run forked a real
restarter — with no test health-port isolation it bound prod's 8102, the
2026-07-24 outage. The top-level `_guard_cluster_spawn` now refuses the call
outright; this replaces the refusal with a recorder for the whole directory, so
no CLI test has to remember the seam and the local leg stays assertable.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def _gate_probe_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the gate observation off this box's real entry port and real launchd.

    `ava status` and the health probe now observe the gate (`probe_gate`), which
    dials `127.0.0.1:<entry port>` and shells out to `launchctl`. On a dev box that
    entry port belongs to the operator's LIVE prod gate and that label is their real
    launchd job, so both seams are stubbed for the whole directory rather than left
    to each test to remember — the same reason `AVA_OS_JOBS_ENABLED=false` exists.

    The default answers are "nothing on the port, no such job", which is what a
    hermetic host looks like. Tests that assert a particular gate state (including
    `_ensure_launchd`'s own) install their own `_launchctl` on top."""
    import cli.commands._converge_gate as cg

    monkeypatch.setattr(cg, "_entry_answers", lambda _port: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        cg,
        "_launchctl",
        lambda *_args: subprocess.CompletedProcess([], 1, "", "no such process"),  # pyright: ignore[reportUnknownArgumentType]
    )


@pytest.fixture(autouse=True)
def local_unpauses(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Records each local unpause the orchestration performs, minus the spawn.

    Clearing the posture is NOT optional bookkeeping: `unpause_local_cluster`
    both writes the `idle` posture and respawns the restarter, and the
    orchestration's `pause_local_cluster` really does write `paused` in the test
    home. A stub that only recorded would leave it behind, and the pause
    middleware then answers 503 to every `TestClient` request for the rest of the
    session — a few hundred unrelated failures, in whichever test file happens to
    run next."""
    calls: list[bool] = []

    def _unpause() -> None:
        # The real unpause writes the R1 host_deploy_state posture row
        # (ops/cluster_pause.py); the stub stands in for that function, so its
        # observable effects must match — a test that asserts the pause
        # lifecycle through the orchestration relies on it, and a leftover
        # `paused` row would 503 every gateway test in this session
        # (host_deploy_state IS in the per-test TRUNCATE list — tests/conftest.py;
        # the R1 singleton deployment_state is not, and self-cleans via
        # acquire/release pairs + test_cluster_lock's _free_lock).
        from shared.host_deploy_state import set_posture

        set_posture("idle")
        calls.append(True)

    monkeypatch.setattr("ops.cluster.unpause_local_cluster", _unpause)
    return calls
