"""Health observations cannot select, roll back, or publish a release."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cli.commands import _cluster_health
from tests.cli.test_cluster_health import (
    _all_checks_pass as _all_checks_pass,
)
from tests.cli.test_cluster_health import (
    _home as _home,
)
from tests.cli.test_cluster_health import (
    _no_deploy_in_flight as _no_deploy_in_flight,
)
from tests.cli.test_cluster_health import (
    _provider_guard_healthy as _provider_guard_healthy,
)
from tests.cli.test_cluster_health import (
    _sent_alerts as _sent_alerts,
)


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
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("candidate", datetime.now(UTC))
    )
    monkeypatch.setattr("shared.cluster_pin.promote_pending_known_good_if_ready", forbidden)
    monkeypatch.setattr("shared.cluster_pin.set_last_known_good_sha", forbidden)
    monkeypatch.setattr("shared.cluster_pin.set_cluster_target_sha", forbidden)
    if health == "gateway":
        monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
        monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: False)
    elif health == "population":
        monkeypatch.setattr(_cluster_health, "_agent_population", no_agents)
        monkeypatch.setattr(_cluster_health, "_agent_population_failure_class", population_failure)
    elif health == "service":
        monkeypatch.setattr(_cluster_health, "_service_probes", lambda: ["frontend (unknown)"])
    elif health == "disk":
        monkeypatch.setattr(_cluster_health, "_disk_usage_failure", lambda: "disk full")
    elif health == "provider":
        monkeypatch.setattr(_cluster_health, "run_provider_guard", provider_failure)

    for _round in range(5):
        assert _cluster_health.run_health_probe() == (0 if health == "healthy" else 1)
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
    import cli.commands._cluster_health as _cluster_health_commands
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
