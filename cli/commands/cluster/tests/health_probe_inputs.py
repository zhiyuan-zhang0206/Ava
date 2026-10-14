"""Explicit operator and captured telemetry inputs for health observations."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.system.hostport import Host
from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.cluster import health as cluster_health
from cli.commands.converge.redis_bridge import RedisBridgeStatus
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database

Probe = Callable[..., int]


def healthy(*_args: object, database_factory: DatabaseFactory) -> bool:
    assert callable(database_factory)
    return True


def unhealthy(*_args: object, database_factory: DatabaseFactory) -> bool:
    assert callable(database_factory)
    return False


def service_probe_verdict(
    *, machine: MachineIdentity, host: Host, failures: list[str]
) -> list[str]:
    assert isinstance(machine, MachineIdentity)
    assert isinstance(host, Host)
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
def schema_verdicts() -> list[bool]:
    """Schema verdicts the patched check returns, oldest first; empty = pass.

    A test appends False to fail one round's schema check while every other
    check of `all_checks_pass` stays healthy."""
    return []


@pytest.fixture
def quiet_schema_health(monkeypatch: pytest.MonkeyPatch, schema_verdicts: list[bool]) -> None:
    """The schema check passes without a real skew, per `schema_verdicts`."""

    def schema_verdict(*_args: object, database_factory: DatabaseFactory) -> bool:
        assert callable(database_factory)
        return schema_verdicts.pop(0) if schema_verdicts else True

    monkeypatch.setattr(cluster_health, "schema_health", schema_verdict)


@pytest.fixture
def quiet_service_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service probes and the Redis bridge pass without dialing anything."""
    monkeypatch.setattr(
        cluster_health, "service_probes", partial(service_probe_verdict, failures=[])
    )
    monkeypatch.setattr(
        cluster_health, "redis_bridge_probe", partial(redis_bridge_verdict, result=None)
    )


@pytest.fixture
def quiet_host_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-level checks (disk, editable records, source tree) pass."""
    monkeypatch.setattr(cluster_health, "disk_usage_failure", lambda: None)
    monkeypatch.setattr(cluster_health, "editable_install_failure", lambda: None)
    # Source-tree cases below supply their own checkout outcomes.
    monkeypatch.setattr(cluster_health, "source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def all_checks_pass(
    monkeypatch: pytest.MonkeyPatch,
    quiet_schema_health: None,
    quiet_service_health: None,
    quiet_host_health: None,
) -> None:
    monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "agent_population", healthy)  # pyright: ignore[reportUnknownArgumentType]


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


@pytest.fixture
def provider_guard_reports() -> list[tuple[str, str]]:
    """Provider-guard failures the stubbed guard reports this round, in order.

    Empty (the default) = the guard passes. A test appends the failures it
    wants reported, e.g. the balance-low signal with the real guard's message
    shape."""
    return []


@pytest.fixture(autouse=True)
def provider_guard_healthy(
    monkeypatch: pytest.MonkeyPatch, provider_guard_reports: list[tuple[str, str]]
) -> None:
    """Checks 9-10 read a live provider API and the real agent table; stub the
    guard so this file's tests stay hermetic. The stub reports the failures in
    `provider_guard_reports` and returns their count, exactly as the real guard
    reports and counts (the real module's own tests exercise its readings)."""

    def _guard(*, report: Callable[[str, str], None], database_factory: DatabaseFactory) -> int:
        assert callable(database_factory)
        for check, message in provider_guard_reports:
            print(message, file=sys.stderr)
            report(check, message)
        return len(provider_guard_reports)

    monkeypatch.setattr(cluster_health, "run_provider_guard", _guard)


@pytest.fixture(autouse=True)
def ran(emitted_events: EmittedEvents) -> FollowedAttributes:
    """The attributes of every `health_probe_ran` heartbeat the probe emits."""
    return emitted_events.following("health_probe_ran")


@pytest.fixture(autouse=True)
def signals(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> Generator[FollowedAttributes]:
    """The attributes of every `health_probe_failing` event the probe emits (heartbeats are `ran`).

    Autouse so no test reaches the real event pipeline. After the test, every event that was
    emitted must be one of the two the probe declares, with the declared category and level."""
    monkeypatch.setattr(cluster_health.telemetry, "init_telemetry", _no_init)
    yield emitted_events.following("health_probe_failing")
    for event in emitted_events:
        assert event.category == "telemetry"
        assert event.event_name in {"health_probe_ran", "health_probe_failing"}
        assert event.level == ("info" if event.event_name == "health_probe_ran" else "warning")
