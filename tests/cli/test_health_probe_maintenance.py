"""A real low-population query cannot turn explicit maintenance into a rollback."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from cli.commands import _cluster_health, _health_alerts
from shared import disabled_services, pause_owner


@pytest.fixture
def probe_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> Path:
    """Keep the real DB/count/journal paths; intercept only external side effects."""
    assert db_conn.execute("SELECT count(*) FROM agents_meta").fetchone() == (0,)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(_cluster_health, "_deploy_suppression", lambda: None)
    (tmp_path / _cluster_health.FAILURE_COUNT_FILE).write_text(
        f"2\ncode\nprevious low population\n{datetime.now(UTC).isoformat()}"
    )
    (tmp_path / _cluster_health.PENDING_LKG_PASSES_FILE).write_text("candidate\n1")
    return tmp_path


@pytest.fixture
def rollbacks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(_health_alerts.subprocess, "run", run)
    return calls


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    emitted: list[dict[str, object]] = []

    def ingest(**kwargs: object) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(_health_alerts, "_ingest_alert", ingest)
    return emitted


@pytest.mark.parametrize("intent", ["disabled", "maintenance"])
def test_expected_low_population_does_not_rollback_or_promote(
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: list[dict[str, object]],
    intent: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if intent == "disabled":
        disabled_services.write_skipped({"agent-host"})
        marker = probe_home / "disabled_services"
    else:
        pause_owner.begin_maintenance("test-maintenance", datetime.now(UTC))
        marker = pause_owner.state_path()
    intent_before = marker.read_bytes()
    # An aged incident would normally alert immediately on this probe.
    started_at = datetime.now(UTC) - timedelta(minutes=20)
    message = "FAIL: agent population — fewer than 1 agent(s) running/idling"
    (probe_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"{message}\n{started_at.isoformat()}\n"
    )

    assert _cluster_health._agent_population(1) is False
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1

    assert rollbacks == []
    assert len(alerts) == 1  # Local intent cannot hide a real global population outage.
    assert alerts[0]["status"] == "firing"
    assert marker.read_bytes() == intent_before
    assert (probe_home / _cluster_health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "0"
    assert not (probe_home / _cluster_health.PENDING_LKG_PASSES_FILE).exists()
    assert "maintenance" in capsys.readouterr().err


@pytest.mark.parametrize(
    "intent", ["absent", "other-service", "legacy-pause", "resumed", "invalid"]
)
def test_unexpected_low_population_still_rolls_back(
    probe_home: Path, rollbacks: list[list[str]], alerts: list[dict[str, object]], intent: str
) -> None:
    holder, acquired_at = "test-maintenance", datetime.now(UTC)
    if intent == "other-service":
        disabled_services.write_skipped({"frontend"})
    elif intent == "legacy-pause":
        pause_owner.mark_paused(holder, acquired_at)
    elif intent == "resumed":
        current = pause_owner.begin_maintenance(holder, acquired_at)
        assert current.maintenance is not None
        pause_owner.change_maintenance(
            holder, acquired_at, current.maintenance, current.maintenance, resumed=True
        )
    elif intent == "invalid":
        marker = pause_owner.state_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("invalid journal")

    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert len(rollbacks) == 1
    assert rollbacks[0][1:] == ["cluster", "rollback", "--yes"]
    assert (probe_home / _cluster_health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "3"


def test_reenabled_host_restores_population_failure_counting(
    probe_home: Path, rollbacks: list[list[str]], alerts: list[dict[str, object]]
) -> None:
    disabled_services.write_skipped({"agent-host"})
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=1) == 1
    assert rollbacks == []

    disabled_services.write_skipped(set())
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=1) == 1
    assert len(rollbacks) == 1
    assert (probe_home / _cluster_health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "1"


def test_disabled_host_does_not_explain_gateway_code_failure(
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_services.write_skipped({"agent-host"})
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: False)

    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert len(rollbacks) == 1


def test_maintenance_does_not_turn_db_failure_into_an_expected_population(
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_services.write_skipped({"agent-host"})

    def down(**_kwargs: object) -> None:
        raise ConnectionError("private test data plane unavailable")

    monkeypatch.setattr("shared.db.connect", down)
    assert _cluster_health._agent_population_failure_class(1) == "environment"
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert rollbacks == []
    assert (probe_home / _cluster_health.FAILURE_COUNT_FILE).read_text().splitlines()[0] == "2"
