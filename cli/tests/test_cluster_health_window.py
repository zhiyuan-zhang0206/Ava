"""Health observations cannot select, roll back, or publish a release."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.test_cluster_health import (
    _all_checks_pass as _all_checks_pass,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _home as _home,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _no_deploy_in_flight as _no_deploy_in_flight,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _provider_guard_healthy as _provider_guard_healthy,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _sent_alerts as _sent_alerts,
)
from cli.commands.cluster.tests.test_cluster_health import _write_aged_alert_state


@pytest.mark.parametrize(
    "health", ["healthy", "gateway", "population", "service", "disk", "provider"]
)
def test_repeated_observations_never_mutate_release_state(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch, health: str
) -> None:
    """Repeated good/bad rounds ignore prior rollback and promotion counters."""
    legacy_state = {
        "health_probe_failures": "999\ncode\nold outage\n2026-09-01T00:00:00+00:00",
        "health_probe_pending_lkg_passes": "candidate\n999",
    }
    for name, content in legacy_state.items():
        (_home / name).write_text(content)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("health observation attempted a release effect")

    def no_agents(_minimum: int) -> bool:
        return False

    def population_failure(_minimum: int) -> str:
        return "code"

    def provider_failure(_home: Path, *, alert_failure: object) -> int:
        return 1

    monkeypatch.setattr(subprocess, "run", forbidden)
    if health == "gateway":
        monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
        monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: False)
    elif health == "population":
        monkeypatch.setattr(cluster_health, "_agent_population", no_agents)
        monkeypatch.setattr(cluster_health, "_agent_population_failure_class", population_failure)
    elif health == "service":
        monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["frontend (unknown)"])
    elif health == "disk":
        monkeypatch.setattr(cluster_health, "_disk_usage_failure", lambda: "disk full")
    elif health == "provider":
        monkeypatch.setattr(cluster_health, "run_provider_guard", provider_failure)

    for _round in range(5):
        assert cluster_health.run_health_probe() == (0 if health == "healthy" else 1)
    assert {name: (_home / name).read_text() for name in legacy_state} == legacy_state


@pytest.mark.parametrize(
    "arguments",
    [
        ["health-probe", "--auto-rollback"],
        ["health-probe", "--threshold", "3"],
        ["health-probe-register", "--threshold", "3"],
    ],
)
def test_parser_rejects_removed_release_policy_flags(arguments: list[str]) -> None:
    from cli.main import _build_parser

    with pytest.raises(SystemExit) as refused:
        _build_parser().parse_args(["cluster", *arguments])
    assert refused.value.code == 2


def test_health_probe_dispatch_preserves_observation_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.commands.cluster.health as _cluster_health_commands
    from cli.main import _build_parser

    received: list[dict[str, object]] = []

    def probe(**kwargs: object) -> int:
        received.append(kwargs)
        return 1

    monkeypatch.setattr(_cluster_health_commands, "cmd_health_probe", probe)
    args = _build_parser().parse_args(
        ["cluster", "health-probe", "--agent-min", "2", "--crash-loop-max-restarts", "4"]
    )
    assert args.func(args) == 1
    assert received == [
        {
            "agent_min": 2,
            "crash_loop_max_restarts": 4,
            "crash_loop_window_minutes": 10,
            "check_crash_loops": True,
            "check_schema": True,
        }
    ]


def test_full_disk_is_reported_when_gateway_is_down(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Disk pressure must explain the gateway outage without being short-circuited."""
    message = "FAIL: disk usage — data volume 92.4% used (watermark 90%)"
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(
        cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )
    _write_aged_alert_state(_home, message)

    assert cluster_health.run_health_probe() == 1
    assert message in capsys.readouterr().err
    assert len(_sent_alerts) == 1
    assert "disk usage" in _sent_alerts[0]
    assert "gateway liveness" not in _sent_alerts[0]


@pytest.mark.parametrize(
    ("flag", "destination"),
    [
        ("crash-loop-check", "crash_loop_check"),
        ("schema-check", "schema_check"),
    ],
)
def test_health_probe_check_flags_disable_only_when_negated(flag: str, destination: str) -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    default = parser.parse_args(["cluster", "health-probe"])
    disabled = parser.parse_args(["cluster", "health-probe", f"--no-{flag}"])
    assert getattr(default, destination) is True
    assert getattr(disabled, destination) is False
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["cluster", "health-probe", f"--{flag}"])
    assert refused.value.code == 2
