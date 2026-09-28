"""An incomplete operation explains an outage only while its executor provably lives.

The finite executor stamps a heartbeat beside its journal while it runs
(`shared.release_operation.executor_heartbeat`). The health probe lets an
incomplete release or PITR operation pause alert grading only while that
stamp is fresh (`operation_in_flight`); a killed, OOM'd or rebooted executor,
or one that never launched, stops stamping, so its operation explains nothing
and the probe reports the executor lost.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cli.commands.cluster import health as cluster_health
from cli.release_fleet.request import FleetRequest
from cli.release_transition.journal import create
from cli.release_transition.request import ReleaseRef
from shared.deploy_timing import EXECUTOR_HEARTBEAT_TTL_S
from shared.release_operation import executor_heartbeat, operation_in_flight
from shared.start_inputs import configuration_digest
from tests.cli.test_cluster_health import _all_checks_pass as _all_checks_pass
from tests.cli.test_cluster_health import _home as _home
from tests.cli.test_cluster_health import _no_deploy_in_flight as _no_deploy_in_flight
from tests.cli.test_cluster_health import _provider_guard_healthy as _provider_guard_healthy
from tests.cli.test_cluster_health import _sent_alerts as _sent_alerts
from tests.cli.test_cluster_health import _write_aged_alert_state

_TTL = timedelta(seconds=EXECUTOR_HEARTBEAT_TTL_S)
_LIVENESS_FAILURE = "FAIL: gateway liveness — health endpoint unreachable or non-200"


def _journal(home: Path, *, created_at: datetime, phase: str) -> Path:
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})
    request = FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=created_at,
        machine="test-unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest=configuration_digest(home),
    )
    (home / "releases").mkdir(exist_ok=True)
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": previous.artifact_digest,
                "manifest_digest": previous.manifest_digest,
            }
        )
    )
    create(request)
    payload = json.loads(request.path.read_bytes())
    payload["phase"] = phase
    request.path.write_text(json.dumps(payload) + "\n")
    return request.path


def _beat(path: Path, at: datetime) -> None:
    (path.parent / "executor-heartbeat").write_text(at.isoformat() + "\n")


def test_an_operation_explains_an_outage_only_within_its_executors_heartbeat(
    tmp_path: Path,
) -> None:
    """Before any beat the operation's creation is its launch grace; after it,
    the last beat. Past the TTL either way the executor is lost."""
    home = tmp_path.resolve()
    created = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    path = _journal(home, created_at=created, phase="stopping")
    label = f"release operation {path.parent.name} at stopping"

    launching = operation_in_flight(home, now=created + _TTL)
    assert launching is not None and (launching.label, launching.alive) == (label, True)
    never_launched = operation_in_flight(home, now=created + _TTL + timedelta(seconds=1))
    assert never_launched is not None and not never_launched.alive
    assert never_launched.last_seen == created

    beat = created + timedelta(hours=1)
    _beat(path, beat)
    alive = operation_in_flight(home, now=beat + _TTL)
    assert alive is not None and alive.alive
    lost = operation_in_flight(home, now=beat + _TTL + timedelta(seconds=1))
    assert lost is not None and not lost.alive and lost.last_seen == beat


def test_the_executor_heartbeat_stamps_while_it_runs_and_leaves_with_it(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    path = _journal(home, created_at=datetime.now(UTC) - 2 * _TTL, phase="stopping")
    before = operation_in_flight(home)
    assert before is not None and not before.alive
    with executor_heartbeat(path):
        running = operation_in_flight(home)
        assert running is not None and running.alive
    assert not (path.parent / "executor-heartbeat").exists()
    left = operation_in_flight(home)
    assert left is not None and not left.alive


def test_a_dead_executor_stops_pausing_grading_and_the_probe_reports_it_lost(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A journal stuck at `stopping` with no executor is an incident: the
    cluster stays down while nothing will ever finish the release."""
    created = datetime.now(UTC) - timedelta(hours=1)
    path = _journal(_home, created_at=created, phase="stopping")
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: False)
    _write_aged_alert_state(_home, _LIVENESS_FAILURE, age=timedelta(minutes=30))

    assert cluster_health.run_health_probe() == 1
    [alert] = _sent_alerts
    assert "operation executor lost" in alert
    assert f"release operation {path.parent.name} at stopping" in alert


def test_a_live_executor_still_pauses_grading(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    path = _journal(_home, created_at=datetime.now(UTC) - timedelta(hours=1), phase="stopping")
    _beat(path, datetime.now(UTC))
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: False)
    _write_aged_alert_state(_home, _LIVENESS_FAILURE, age=timedelta(minutes=30))

    assert cluster_health.run_health_probe() == 1
    assert _sent_alerts == []


def test_a_recovering_operation_explains_nothing_past_its_first_phase(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A recovery's hold is itself worth an alert. The decision records an
    error, but the recovery's first `advance` clears it; the journaled
    decision is what keeps the operation from explaining the outage."""
    path = _journal(_home, created_at=datetime.now(UTC) - timedelta(hours=1), phase="fencing")
    payload = json.loads(path.read_bytes())
    payload["direction"] = "previous"
    decided = datetime.now(UTC) - timedelta(minutes=20)
    payload["fleet"]["decisions"] = [
        {
            "kind": "recover",
            "phase": "starting",
            "reason": "candidate failed",
            "at": decided.isoformat(),
        }
    ]
    path.write_text(json.dumps(payload) + "\n")
    _beat(path, datetime.now(UTC))
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: False)
    _write_aged_alert_state(_home, _LIVENESS_FAILURE, age=timedelta(minutes=30))

    assert cluster_health.run_health_probe() == 1
    [alert] = _sent_alerts
    assert "gateway liveness" in alert
