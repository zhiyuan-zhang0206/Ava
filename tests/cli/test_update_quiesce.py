"""Updates use the native pause barrier, and never migrate on an incomplete drain."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cli.commands as _cli
from cli.commands import update as _up
from ops import agent_pause
from shared.config import settings


@pytest.mark.parametrize("mode", ["smooth", "force", "none"])
def test_every_update_mode_uses_native_pause(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    pause = MagicMock()
    monkeypatch.setattr(agent_pause, "pause_agents", pause)
    assert _up._quiesce_local_agents(mode)
    pause.assert_called_once_with(settings.gateway.update_quiesce_timeout_seconds)


def test_timeout_is_not_force_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    pause = MagicMock(side_effect=TimeoutError("still flushing"))
    reap = MagicMock()
    monkeypatch.setattr(agent_pause, "pause_agents", pause)
    monkeypatch.setattr(_cli, "_kill_session", reap)
    with pytest.raises(TimeoutError, match="still flushing"):
        _up._quiesce_all_agents(0.1)
    reap.assert_not_called()


@pytest.mark.parametrize("status", ["fatal", "unreachable"])
def test_any_missing_remote_drain_aborts(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    from cli.commands._update_pause import _run_phase_a
    from tests.agent.test_maintenance import WHEN

    monkeypatch.setattr(
        _cli, "_fan_out", MagicMock(return_value=[("runner", status, "no receipt")])
    )
    assert (
        _run_phase_a(
            [("runner", "http://unused")],
            deploy_capability={
                "deploy_holder": "test",
                "deploy_acquired_at": WHEN.isoformat(),
            },
        )
        is None
    )


def test_unreachable_fetch_cannot_skip_a_live_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands._update_preflight import _run_preflight_fetch

    monkeypatch.setattr(
        _cli, "_fan_out", MagicMock(return_value=[("runner", "unreachable", "offline")])
    )
    assert _run_preflight_fetch([("runner", "http://unused")], restart_only=False)


def test_orchestration_quiesces_after_phase_a_before_local_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_gateway_orchestration` must call `_quiesce_all_agents`
    strictly between Phase A (local pause + cluster/stop fan-out) and the local
    update (which migrates) — no old-code agent may be live during the migration.
    Local host admission closes before remote fan-out, so no new local turn
    can start while the remote barrier is still pending."""
    order: list[str] = []

    # backend change → full orchestration (avoid a real git fetch in the test)
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    # This test asserts quiesce ordering, not the migration-layout vet; stub the vet
    # so it does not git-ls-tree the real origin/main (whose layout is orthogonal here).
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("wsl", "http://unused")])

    def _fan_out(_hosts, path, _timeout, payload=None):
        order.append(f"fan_out:{path}")
        return [("wsl", "ok", "")]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: order.append("pause_local"))
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: order.append("quiesce") or True)  # pyright: ignore[reportUnknownArgumentType]

    def _local(_repo, **_kw):
        order.append("local_update")
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: order.append("poll") or {},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0

    a_idx = order.index("fan_out:/api/cluster/stop")
    p_idx = order.index("pause_local")
    q_idx = order.index("quiesce")
    l_idx = order.index("local_update")
    assert p_idx < a_idx < q_idx < l_idx, (
        f"pause_local must run before Phase A, quiesce after both; got {order}"
    )


def test_orchestration_quiesces_even_without_agent_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-host path (no agent-runners registered) pauses the local restarter,
    then quiesces local agents before migrating — the local restarter must not
    respawn quiesced agents while the gateway is updating."""
    order: list[str] = []

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(
        _up,
        "_vet_rollout_target",
        lambda _sha: None,  # pyright: ignore[reportUnknownArgumentType]
    )  # ordering test, not the vet  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: order.append("pause_local"))
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: order.append("quiesce") or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **_kw: order.append("local_update") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert order == ["pause_local", "quiesce", "local_update"]


def test_orchestration_aborts_when_update_lock_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second gateway update aborts (rc=1) without running the inner
    orchestration when a live holder already owns the cluster update lock — the
    serialization that stops two rollouts advancing the schema at once
    (the 2026-06-01 collision)."""
    from shared.cluster_lock import acquire_update_lock, release_update_lock

    ran: list[bool] = []
    monkeypatch.setattr(
        _up,
        "_run_gateway_orchestration_inner",
        lambda *_a, **_kw: ran.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert acquire_update_lock("other-holder") is True
    try:
        rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
        assert rc == 1
        assert ran == []  # inner orchestration never ran
    finally:
        release_update_lock("other-holder")


def test_local_and_remote_phase_a_share_one_deploy_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from cli.commands._update_pause import _stop_the_world
    from ops import ops_cluster
    from shared import maintenance, pause_owner
    from shared.maintenance_state import MaintenanceHold
    from tests.agent.test_maintenance import WHEN

    def drain() -> None:
        owner = pause_owner.read()
        assert owner.holder == "deployment" and owner.acquired_at == WHEN
        before = pause_owner.begin_maintenance("deployment", WHEN)
        assert before.maintenance is not None
        if before.maintenance.phase == "preparing":
            pause_owner.change_maintenance(
                "deployment", WHEN, before.maintenance, MaintenanceHold("drained")
            )

    def fanout(
        _hosts: object, _path: str, _timeout: float, payload: dict[str, str]
    ) -> list[tuple[str, str, str]]:
        ops_cluster.cluster_stop_op(
            payload["deploy_holder"], datetime.fromisoformat(payload["deploy_acquired_at"])
        )
        return [("local", "ok", "drained"), ("remote", "ok", "drained")]

    monkeypatch.setattr("ops.cluster.pause_local_cluster", drain)
    monkeypatch.setattr(ops_cluster, "pause_local_cluster", drain)
    monkeypatch.setattr(ops_cluster, "_require_executing_deploy", MagicMock())
    monkeypatch.setattr(_cli, "_quiesce_all_agents", MagicMock(return_value=True))
    monkeypatch.setattr(_cli, "_fan_out", fanout)
    acked, drained_all = _stop_the_world(
        [("local", None), ("remote", None)],
        deploy_capability={"deploy_holder": "deployment", "deploy_acquired_at": WHEN.isoformat()},
    )
    assert acked == {"local", "remote"} and drained_all and maintenance.held()
