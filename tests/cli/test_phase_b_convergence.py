"""Phase B needs completed native startup and running code, not an idle DB row."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cli.commands._update_phase_b import POLL_OK, _probe_verdict
from ops import cluster_pause, cluster_status
from shared import host_deploy_state, maintenance, pause_owner, start_serving
from shared.machine import machine_name

SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolated_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(start_serving, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(cluster_status, "_collect_sessions", lambda: ([], 0, 0))

    def live_pidfile(_path: str) -> tuple[bool, int]:
        return True, 1

    def no_outcome(_state: object) -> None:
        return None

    monkeypatch.setattr(cluster_status, "_check_pidfile", live_pidfile)
    monkeypatch.setattr(cluster_status, "_read_resource_sample", lambda: None)
    monkeypatch.setattr(cluster_status, "last_updater_outcome", no_outcome)
    monkeypatch.setattr("shared.cluster_drift.prod_source_head_sha", lambda: SHA)
    monkeypatch.setattr("shared.process_sha.get", lambda: SHA)
    host_deploy_state.set_posture("idle")
    host_deploy_state.clear_updater_lease()


def _status() -> dict[str, Any]:
    return cluster_status.status_snapshot().model_dump(mode="json")


def test_missing_deploy_row_is_not_convergence(db_conn: psycopg.Connection[Any]) -> None:
    start_serving.mark_serving(start_serving.begin_start())
    db_conn.execute("DELETE FROM host_deploy_state WHERE machine=%s", (machine_name(),))
    db_conn.commit()
    verdict, *_ = _probe_verdict(_status(), 0, machine_name(), 0)
    assert verdict is None


def test_same_pin_restart_waits_for_native_hold_and_real_serving() -> None:
    """An old serving marker and idle posture survive Phase A; neither is completion."""
    start_serving.mark_serving(start_serving.begin_start())
    at = datetime.now(UTC)
    pause_owner.begin_maintenance("phase-b", at)
    assert not cluster_pause.is_paused()  # Business API policy is unchanged.
    assert maintenance.held()
    result = _status()
    assert result["paused"] is True
    assert _probe_verdict(result, 0, machine_name(), 0)[0] is None

    generation = start_serving.begin_start()
    # Cancel the empty admission hold through the normal resume function.
    # Startup readiness must still independently refuse convergence.
    from ops.agent_pause import resume_agents

    resume_agents()
    assert not maintenance.held()
    assert _status()["paused"] is True
    assert _probe_verdict(_status(), 0, machine_name(), 0)[0] is None

    start_serving.mark_serving(generation)
    result = _status()
    assert result["paused"] is False
    verdict = _probe_verdict(result, 0, machine_name(), 0)[0]
    assert verdict is not None and verdict.status == POLL_OK


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("running_sha", "b" * 40),
        ("head_sha", None),
        ("running_sha", None),
        ("head_sha", ""),
        ("running_sha", ""),
        ("paused", None),
    ],
)
def test_partial_or_stale_probe_is_not_convergence(field: str, value: object) -> None:
    start_serving.mark_serving(start_serving.begin_start())
    result = _status()
    result[field] = value
    assert _probe_verdict(result, 0, machine_name(), 0)[0] is None


def test_idle_with_live_updater_is_not_convergence() -> None:
    start_serving.mark_serving(start_serving.begin_start())
    host_deploy_state.touch_updater_lease()
    host_deploy_state.set_posture("idle")
    assert _probe_verdict(_status(), 0, machine_name(), 0)[0] is None


@pytest.mark.parametrize("paused", [0, "false", "", None])
def test_pause_must_be_an_explicit_boolean(paused: object) -> None:
    result = {"head_sha": SHA, "running_sha": SHA, "paused": paused}
    assert _probe_verdict(result, 0, machine_name(), 0, target_sha=SHA)[0] is None


def test_matching_old_process_and_checkout_do_not_satisfy_target() -> None:
    result = {"head_sha": "b" * 40, "running_sha": "b" * 40, "paused": False}
    assert _probe_verdict(result, 0, machine_name(), 0, target_sha=SHA)[0] is None


def test_resume_before_lease_clear_ignores_stale_terminal_outcome() -> None:
    start_serving.mark_serving(start_serving.begin_start())
    host_deploy_state.touch_updater_lease()
    host_deploy_state.set_posture("idle")
    result = _status()
    result["last_updater_outcome"] = {"kind": "exited", "rc": 1}
    for _ in range(3):
        verdict, stalls, _, _ = _probe_verdict(
            result, 1, machine_name(), 0, poll_elapsed=300, target_sha=SHA
        )
        assert verdict is None and stalls == 0
    host_deploy_state.clear_updater_lease()
    verdict = _probe_verdict(result, 0, machine_name(), 0, target_sha=SHA)[0]
    assert verdict is not None and verdict.status == POLL_OK


@pytest.mark.parametrize("restart_only", [False, True])
def test_phase_b_polls_the_dispatched_target(
    monkeypatch: pytest.MonkeyPatch, restart_only: bool
) -> None:
    """Reject matching old code on update, and retained native hold on same-pin restart."""
    from cli import commands
    from cli.commands._update_phase_b import _phase_b_and_poll
    from ops import cluster_rpc

    start_serving.mark_serving(start_serving.begin_start())
    probes = 0

    def fanout(
        hosts: object, path: str, timeout: float, *, payload: object
    ) -> list[tuple[str, str, str]]:
        assert payload == ({"restart_only": True} if restart_only else {"target_sha": SHA})
        return [(machine_name(), "ok", "")]

    async def probe(**_kwargs: object) -> dict[str, object]:
        nonlocal probes
        probes += 1
        code = "b" * 40 if probes == 1 and not restart_only else SHA
        return {"head_sha": code, "running_sha": code, "paused": restart_only and probes == 1}

    monkeypatch.setattr(commands, "_fan_out", fanout)
    monkeypatch.setattr(commands, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(commands, "_POLL_TIMEOUT_S", 2)
    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", probe)
    result = _phase_b_and_poll(
        [(machine_name(), None)],
        target_sha=None if restart_only else SHA,
        restart_only=restart_only,
    )
    assert result[machine_name()].status == POLL_OK
    assert probes == 2
