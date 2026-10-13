"""Explicit operator and captured telemetry inputs for health observations."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from cli.commands.cluster import health as cluster_health
from cli.commands.converge.redis_bridge import RedisBridgeStatus
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database

Probe = Callable[..., int]


def healthy(*_args: object, database_factory: DatabaseFactory) -> bool:
    assert callable(database_factory)
    return True


def unhealthy(*_args: object, database_factory: DatabaseFactory) -> bool:
    assert callable(database_factory)
    return False


def service_probe_verdict(*, machine: MachineIdentity, failures: list[str]) -> list[str]:
    assert isinstance(machine, MachineIdentity)
    return failures


def bridge_status(
    *,
    required: bool = True,
    endpoint: str = "10.64.0.7:6380",
    serving: bool = True,
    detail: str = "Redis PING succeeded",
) -> RedisBridgeStatus:
    return RedisBridgeStatus(required, endpoint, serving, detail)


def redis_bridge_verdict(
    *, machine: MachineIdentity, config: ConfigBoot, result: str | None
) -> str | None:
    assert isinstance(machine, MachineIdentity)
    assert isinstance(config, ConfigBoot)
    return result


@pytest.fixture
def all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "agent_population", healthy)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "schema_health", healthy)
    monkeypatch.setattr(
        cluster_health, "service_probes", partial(service_probe_verdict, failures=[])
    )
    monkeypatch.setattr(
        cluster_health, "redis_bridge_probe", partial(redis_bridge_verdict, result=None)
    )
    monkeypatch.setattr(cluster_health, "disk_usage_failure", lambda: None)
    monkeypatch.setattr(cluster_health, "editable_install_failure", lambda: None)
    # Source-tree cases below supply their own checkout outcomes.
    monkeypatch.setattr(cluster_health, "source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def probe(operator_database: Callable[[], Any], command_runtime: CommandRuntime) -> Probe:
    # This file replaces telemetry initialization; supply its retained input without workers.
    pipeline = object()
    return partial(
        cluster_health.run_health_probe,
        database_factory=operator_database,
        producer=lambda: pipeline,
        command=command_runtime,
    )


@pytest.fixture(autouse=True)
def config_boot_environment() -> Generator[None]:
    """Restore process delivery from the health operation's boot."""
    with patch.dict(os.environ):
        yield


def _no_init(**_kwargs: object) -> None:
    return None


@pytest.fixture(autouse=True)
def provider_guard_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checks 9-10 read a live provider API and the real agent table; stub the
    guard healthy everywhere so this file's tests stay hermetic. The guard's
    own tests below re-point `run_provider_guard` at the real one to exercise
    the wiring."""

    def _ok(*, report: object, database_factory: DatabaseFactory) -> None:
        assert callable(database_factory)

    monkeypatch.setattr(cluster_health, "run_provider_guard", _ok)


@pytest.fixture(autouse=True)
def ran() -> list[dict[str, object]]:
    """The attributes of every `health_probe_ran` heartbeat the probe emits."""
    return []


@pytest.fixture(autouse=True)
def signals(
    monkeypatch: pytest.MonkeyPatch, ran: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Capture the attributes of every `health_probe_failing` event the probe emits
    (heartbeats go to `ran`).

    Autouse so no test reaches the real event pipeline."""
    emitted: list[dict[str, object]] = []

    def _emit(category: str, event_name: str, **kwargs: object) -> None:
        assert category == "telemetry"
        attributes = kwargs["attributes"]
        assert isinstance(attributes, dict)
        if event_name == "health_probe_ran":
            assert "level" not in kwargs  # info
            ran.append(dict(attributes))  # pyright: ignore[reportUnknownArgumentType]
            return
        assert event_name == "health_probe_failing"
        assert kwargs["level"] == "warning"
        emitted.append(dict(attributes))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(cluster_health.telemetry, "emit", _emit)
    monkeypatch.setattr(cluster_health.telemetry, "init_telemetry", _no_init)
    return emitted
