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


# ─── alert grading during a deploy ───────────────────────────────────────


def test_deploy_window_tracks_episode_and_grades_after_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sent_alerts: list[dict[str, Any]]
) -> None:
    """A live deploy explains but does not erase an outage episode."""
    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IN_FLIGHT)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)

    assert health.run_health_probe() == 1
    marker = tmp_path / health.ALERT_STATE_FILE
    state = marker.read_text().split("\n")
    assert state[-1] == ""
    state[-2] = (datetime.now(UTC) - timedelta(seconds=601)).isoformat()
    marker.write_text("\n".join(state))

    assert health.run_health_probe() == 1
    assert _sent_alerts == []

    monkeypatch.setattr("ops.deploy_window.deploy_in_flight", lambda **_k: _IDLE)  # pyright: ignore[reportUnknownArgumentType]
    assert health.run_health_probe() == 1
    assert [(edge["severity"], edge["starts_at"]) for edge in _sent_alerts] == [
        ("error", datetime.fromisoformat(state[-2]))
    ]


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
    monkeypatch.setattr("shared.machines.list_all", list)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(health, "_ingest_alert", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    assert health.run_health_probe() == 1


# ─── the orchestration converts its lease into a settle hold ─────────────────


def test_release_settle_hold_never_touches_an_executing_lease() -> None:
    """`settle_hosts IS NOT NULL` in the WHERE clause is what stops a convergence
    check from unlocking a rollout that is actively executing."""
    import inspect

    from shared import cluster_lock

    sql = inspect.getsource(cluster_lock.release_settle_hold)
    assert "settle_hosts IS NOT NULL" in sql
    assert "holder = %s" in sql


def test_settle_hold_leaves_the_holder_string_parseable() -> None:
    """`ops.ops_cluster._lock_holder_is_live` parses the holder as `<machine>:pid<N>`.
    A holder decorated with the reason would fail that parse, read as live, and make
    `ava cluster recover` refuse to break a hold whose owner is provably dead — which
    is why the reason lives in `settle_note`."""
    import inspect

    from shared import cluster_lock

    src = inspect.getsource(cluster_lock.settle_update_lock)
    assert "SET expires_at" in src
    assert "holder = %s" in src and "SET holder" not in src


# ─── the poll renews the lease it is running under ───────────────────────────
