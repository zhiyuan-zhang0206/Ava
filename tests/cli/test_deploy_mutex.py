"""Deploy mutual exclusion at the two actors that can move the cluster pin.

The lease existed and every automated healer already read it; the defect was that
its protected window was shorter than the dangerous one, and that the health probe
was the single actor never consulting it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.commands import _cluster_health as health
from cli.commands import _health_alerts as alerts
from ops import cluster as cluster_mod
from ops import cluster_session
from ops.deploy_window import DeployWindow

_IN_FLIGHT = DeployWindow(
    active=True, detail="a cluster deploy is still settling — gateway-host:pid81319 (held 5m)"
)
_IDLE = DeployWindow(active=False, detail="no deploy in flight")


@pytest.fixture(autouse=True)
def _sent_alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture owner alerts; autouse so no test here publishes to a live
    notification channel. Probe failure paths call `_ingest_alert` (W16: the
    alerts ingest seam — gateway POST, local fallback, im_bridge /send).
    In a unit test the gateway is unreachable and the fallback degrades to
    direct IM, so without a stub these tests dial the real local daemon with
    the pinned fake cluster secret and log a 401 every run — and pre-W16
    (Task #794, 2026-08-05) the direct path read `settings.telegram`, which a
    dev box's shell leak of ~/.ava/.env made a LIVE bot token: these tests
    sent the operator real "[test_...] [health-probe] cluster unhealthy"
    messages, four per local pytest run. The alert semantics stay testable
    through the captured list."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts, "_ingest_alert", lambda **kw: sent.append(kw))  # pyright: ignore[reportUnknownArgumentType]
    return sent


@pytest.fixture(autouse=True)
def _healthy_data_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises deploy suppression, not live Postgres/Redis availability."""
    monkeypatch.setattr(health, "_data_plane_abnormal", lambda: False)


# ─── the human/agent actor: a second deploy is refused, legibly ──────────────


@pytest.fixture
def no_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]


def test_live_orchestration_session_ignores_a_live_rollout_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"ava-rollout-dryrun"}
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", live.__contains__)

    assert cluster_session.live_orchestration_session() is None


def test_live_orchestration_session_still_returns_a_live_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"ava-rollout"}
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", live.__contains__)

    assert cluster_session.live_orchestration_session() == "ava-rollout"


def test_a_live_rollout_dry_run_does_not_block_a_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"ava-rollout-dryrun"}
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", live.__contains__)
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]

    cluster_mod._assert_no_orchestration_in_flight()


def test_a_live_rollout_still_blocks_a_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    live = {"ava-rollout"}
    monkeypatch.setattr(cluster_session, "_has_orchestration_session", live.__contains__)

    with pytest.raises(cluster_mod.ClusterUpdateInProgress, match="ava-rollout"):
        cluster_mod._assert_no_orchestration_in_flight()


def test_second_deploy_is_refused_while_the_cluster_is_still_settling(
    no_local_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-07-29 shape: the orchestration has returned, so this host is clean —
    but the cluster has not converged and the lease is still held."""
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IN_FLIGHT)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(cluster_mod.ClusterUpdateInProgress) as exc:
        cluster_mod._assert_no_orchestration_in_flight()

    message = str(exc.value)
    assert "gateway-host:pid81319" in message, "the refusal must name WHO holds the cluster"
    assert "--force" in message


def test_a_converged_cluster_is_not_refused(
    no_local_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    cluster_mod._assert_no_orchestration_in_flight()  # must not raise


def test_force_overrides_the_cluster_wide_refusal(
    no_local_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _never(**_k: object) -> DeployWindow:
        raise AssertionError("--force must not even ask the cluster-wide question")

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", _never)
    cluster_mod._assert_no_orchestration_in_flight(force=True)  # must not raise


def test_force_does_not_override_a_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two orchestrations on ONE host is never intended, and that check has a precise
    remedy — so `--force` does not skip it."""
    monkeypatch.setattr(
        cluster_session,
        "_has_orchestration_session",
        lambda s: s.endswith("rollout"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    with pytest.raises(cluster_mod.ClusterUpdateInProgress) as exc:
        cluster_mod._assert_no_orchestration_in_flight(force=True)
    assert "orchestration session" in str(exc.value)


def test_refusal_reaches_the_operator_as_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The live defect this fixes: `ava cluster update` used to raise
    `ClusterUpdateInProgress` straight out of the dispatch — `main()` catches
    only `ValidationError`, so a second operator got a stack trace where the
    entire point was a legible refusal. The thin-client POST (issue #216)
    translates the gateway's 409 into the same one-line stderr verdict."""

    class _Refused:
        status_code = 409

        def raise_for_status(self) -> None:
            import httpx

            request = httpx.Request("POST", "http://gw:8000")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("conflict", request=request, response=response)

        def json(self) -> dict[str, str]:
            return {"detail": "a deploy is in progress — gateway-host:pid1"}

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: _Refused())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update() == 1
    assert "gateway-host:pid1" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_nothing_to_update_is_reported_as_success(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """An up-to-date cluster is not a failure; it also used to traceback. The
    gateway's 422 (nothing to roll out) maps to exit 0 with a one-line note."""

    class _Nothing:
        status_code = 422

        def raise_for_status(self) -> None:
            import httpx

            request = httpx.Request("POST", "http://gw:8000")
            response = httpx.Response(422, request=request)
            raise httpx.HTTPStatusError("nothing", request=request, response=response)

        def json(self) -> dict[str, str]:
            return {"detail": "cluster is already up to date"}

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: _Nothing())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update() == 0
    assert "up to date" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ─── the automated actor: auto-rollback does not fire mid-deploy ─────────────


def test_deploy_in_flight_does_not_advance_the_rollback_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core fix: services down because a deploy is running is the expected state,
    not a health failure. Counting it walks an unattended rollback toward firing
    against the rollout that is still running."""
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IN_FLIGHT)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)
    rollbacks: list[object] = []
    monkeypatch.setattr(alerts.subprocess, "run", lambda *_a, **_k: rollbacks.append(True))  # pyright: ignore[reportUnknownArgumentType]

    for _ in range(5):  # well past the default threshold of 3
        assert health.run_health_probe(auto_rollback=True, threshold=3) == 1

    assert rollbacks == []
    counter = tmp_path / health.FAILURE_COUNT_FILE
    assert not counter.exists() or counter.read_text().splitlines()[0] == "0", (
        "the counter never advanced"
    )


def test_a_deploy_resets_the_consecutive_counter_rather_than_freezing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Consecutive" has to mean consecutive. Two real failures, then a deploy, then one
    more failure must NOT reach a threshold of 3: the first two were evidence about the
    commit the deploy has since replaced, and rolling back to "last known good" on their
    strength rolls back code they never observed."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)
    rollbacks: list[object] = []
    monkeypatch.setattr(alerts.subprocess, "run", lambda *_a, **_k: rollbacks.append(True))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    for _ in range(2):
        assert health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert rollbacks == []  # 2 of 3

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IN_FLIGHT)  # pyright: ignore[reportUnknownArgumentType]
    assert health.run_health_probe(auto_rollback=True, threshold=3) == 1

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    assert health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert rollbacks == [], "the post-deploy failure restarted the count at 1"
    assert (tmp_path / health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "1"


def test_deploy_window_tracks_episode_and_grades_after_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sent_alerts: list[dict[str, Any]]
) -> None:
    """A live deploy explains but does not erase an outage episode."""
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IN_FLIGHT)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)

    assert health.run_health_probe(auto_rollback=False) == 1
    marker = tmp_path / health.ALERT_STATE_FILE
    state = marker.read_text().split("\n")
    assert state[-1] == ""
    state[-2] = (datetime.now(UTC) - timedelta(seconds=601)).isoformat()
    marker.write_text("\n".join(state))

    assert health.run_health_probe(auto_rollback=False) == 1
    assert _sent_alerts == []

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    assert health.run_health_probe(auto_rollback=False) == 1
    assert [(edge["severity"], edge["starts_at"]) for edge in _sent_alerts] == [
        ("error", datetime.fromisoformat(state[-2]))
    ]


def test_counter_and_rollback_still_work_with_no_deploy_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's whole purpose must survive the fix."""
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(health, "_ingest_alert", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    ran: list[list[str]] = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(alerts.subprocess, "run", lambda argv, **_k: ran.append(argv) or _Ok())  # pyright: ignore[reportUnknownArgumentType]

    assert health.run_health_probe(auto_rollback=True, threshold=2) == 1
    assert ran == []  # 1/2
    assert health.run_health_probe(auto_rollback=True, threshold=2) == 1
    assert ran and ran[0][1:] == ["cluster", "rollback", "--yes"]


def test_an_unreadable_lease_does_not_suppress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Cannot prove a deploy is running" must mean "assume none is" — a probe that
    goes quiet the moment its evidence source breaks is the failure it exists to
    catch."""
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    monkeypatch.setattr("shared.machines.list_all", list)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(health, "_ingest_alert", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(alerts.subprocess, "run", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    assert health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert (tmp_path / health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "1"


# ─── the orchestration converts its lease into a settle hold ─────────────────


def test_acked_but_unconverged_hosts_are_what_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both non-OK poll verdicts settle: still-converging AND stalled hosts have a
    moved checkout and unswapped processes, which is the window a second deploy must
    not start into. A host that never acked never began transitioning, so a
    decommissioned runner cannot hold the cluster every rollout."""
    from cli.commands.update import (
        POLL_CONVERGING,
        POLL_OK,
        POLL_STALLED,
        PollVerdict,
        _still_converging,
    )

    polls = {
        "win": PollVerdict(POLL_CONVERGING),
        "air": PollVerdict(POLL_STALLED),
        "mini": PollVerdict(POLL_OK),
        "gone": PollVerdict("unreachable"),
        "bad": PollVerdict("fatal"),
    }
    assert sorted(_still_converging(polls)) == ["air", "win"]


def test_release_settle_hold_never_touches_an_executing_lease() -> None:
    """`note IS NOT NULL` in the WHERE clause is what stops a convergence check from
    unlocking a rollout that is actively executing."""
    import inspect

    from shared import cluster_lock

    sql = inspect.getsource(cluster_lock.release_settle_hold)
    assert "note IS NOT NULL" in sql
    assert "holder = %s" in sql


def test_settle_hold_leaves_the_holder_string_parseable() -> None:
    """`ops.ops_cluster._lock_holder_is_live` parses the holder as `<machine>:pid<N>`.
    A holder decorated with the reason would fail that parse, read as live, and make
    `ava cluster recover` refuse to break a hold whose owner is provably dead — which
    is why the reason lives in `note`."""
    import inspect

    from shared import cluster_lock

    src = inspect.getsource(cluster_lock.settle_update_lock)
    assert "SET expires_at" in src
    assert "holder = %s" in src and "SET holder" not in src


# ─── the poll renews the lease it is running under ───────────────────────────


def test_the_phase_b_poll_renews_the_lease_it_runs_under(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant in the one place it can be broken: the poll is the phase that waits
    on other machines, so it is the phase during which a fixed TTL would lapse. It must
    renew for its own holder — the process that called `acquire_update_lock` — and stop
    renewing when it returns, so a crashed rollout still lapses."""
    from datetime import UTC, datetime, timedelta

    import cli.commands as _cli
    from ops import cluster_rpc as cr
    from shared.cluster_lock import self_holder
    from shared.host_deploy_state import HostDeployState

    renewals: list[str] = []
    monkeypatch.setattr("shared.cluster_lock.renew_update_lock", lambda h, **_k: renewals.append(h))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.deploy_timing.LEASE_RENEW_INTERVAL_S", 0.01)

    async def _paused_forever(
        *, target_machine, kind, payload, timeout_s, ops_url=None, retries=None
    ):  # type: ignore[no-untyped-def]
        assert retries == 0
        return {}

    def _fake_read(machine=None, **_kw):
        # A live updater lease: the host is mid-transition the whole poll.
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _paused_forever)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])

    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}
    assert renewals, "the poll must keep its own lease alive while it waits"
    assert set(renewals) == {self_holder()}
