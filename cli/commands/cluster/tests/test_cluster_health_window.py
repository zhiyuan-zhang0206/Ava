import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from base.telemetry.tests.capture.event_capture import FollowedAttributes
from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.health_probe_inputs import (
    Probe,
    service_probe_verdict,
    unhealthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    all_checks_pass as all_checks_pass,
)
from cli.commands.cluster.tests.health_probe_inputs import emitted_events as emitted_events
from cli.commands.cluster.tests.health_probe_inputs import probe as probe
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_healthy as provider_guard_healthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_reports as provider_guard_reports,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_host_health as quiet_host_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_schema_health as quiet_schema_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_service_health as quiet_service_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    ran as ran,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    schema_verdicts as schema_verdicts,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    signals as signals,
)
from cli.commands.cluster.tests.test_cluster_health import (
    isolated_cluster_home as isolated_cluster_home,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.mark.parametrize(
    "health", ["healthy", "gateway", "population", "service", "disk", "provider"]
)
def test_repeated_observations_never_mutate_release_state(
    probe: Probe,
    all_checks_pass: None,
    isolated_cluster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    health: str,
) -> None:
    legacy_state = {
        "health_probe_failures": "999\ncode\nold outage\n2026-09-01T00:00:00+00:00",
        "health_probe_pending_lkg_passes": "candidate\n999",
    }
    for name, content in legacy_state.items():
        (isolated_cluster_home / name).write_text(content)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("health observation attempted a release effect")

    def no_agents(_minimum: int, *, database_factory: Callable[[], Any]) -> bool:
        assert callable(database_factory)
        return False

    def population_failure(_minimum: int, *, database_factory: Callable[[], Any]) -> str:
        assert callable(database_factory)
        return "code"

    def provider_failure(*, report: object, database_factory: Callable[[], Any]) -> int:
        assert callable(database_factory)
        return 1

    monkeypatch.setattr(subprocess, "run", forbidden)
    if health == "gateway":
        monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: False)
        monkeypatch.setattr(cluster_health, "data_plane_abnormal", unhealthy)
    elif health == "population":
        monkeypatch.setattr(cluster_health, "agent_population", no_agents)
        monkeypatch.setattr(cluster_health, "agent_population_failure_class", population_failure)
    elif health == "service":
        monkeypatch.setattr(
            cluster_health,
            "service_probes",
            partial(service_probe_verdict, failures=["frontend (unknown)"]),
        )
    elif health == "disk":
        monkeypatch.setattr(cluster_health, "disk_usage_failure", lambda: "disk full")
    elif health == "provider":
        monkeypatch.setattr(cluster_health, "run_provider_guard", provider_failure)

    for _round in range(5):
        assert probe() == (0 if health == "healthy" else 1)
    assert {
        name: (isolated_cluster_home / name).read_text() for name in legacy_state
    } == legacy_state


def test_full_disk_is_reported_when_gateway_is_down(
    db_url: str,
    redis_url: str,
    probe: Probe,
    all_checks_pass: None,
    isolated_cluster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: FollowedAttributes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(
        cluster_health, "disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )

    assert probe() == 1
    err = capsys.readouterr().err
    assert "FAIL: disk usage — data volume 92.4% used (watermark 90%)" in err
    assert "FAIL: gateway liveness" in err
    assert [signal["check"] for signal in signals] == ["disk_usage", "gateway_liveness"]
