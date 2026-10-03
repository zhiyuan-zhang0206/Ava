"""The OTLP export gate: which identities, roles and endpoints enable export."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import observability
from base.telemetry.otlp import telemetry_otlp


@pytest.fixture(autouse=True)
def _production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most cases exercise an allowed production backend; gate tests override it."""
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_observability_export_gate() -> Any:
    """The production gate is process-cached; tests model fresh processes."""
    gate = telemetry_otlp.observability_export_allowed
    gate.cache_clear()
    yield
    gate.cache_clear()


@pytest.mark.parametrize(
    ("machine_registered", "cluster", "expected"),
    [
        (True, ".ava", True),
        (True, "home", False),
        (True, ".unknown", False),
        (True, "ava_test_home_123", False),
        (False, ".ava", False),
    ],
)
def test_production_identity_requires_registered_machine_and_production_cluster(
    monkeypatch: pytest.MonkeyPatch,
    machine_registered: bool,
    cluster: str,
    expected: bool,
) -> None:
    from base.cluster import machine

    if machine_registered:
        monkeypatch.setattr(machine, "machine_name", lambda: "registered-runner")
    else:

        def missing_machine_name() -> str:
            raise machine.MachineNameMissing("machine name unavailable")

        monkeypatch.setattr(machine, "machine_name", missing_machine_name)
    monkeypatch.setattr(observability, "cluster_label", lambda: cluster)

    assert observability.production_identity() is expected


@pytest.mark.parametrize("configured", [False, True])
def test_enabled_follows_setting_outside_exec_child(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", configured)

    assert telemetry_otlp._OtlpBackend._enabled() is configured


@pytest.mark.parametrize(
    ("marker", "endpoint_override", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_gateway_export_gate_requires_lgtm_marker_or_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker: bool,
    endpoint_override: bool,
    expected: bool,
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    if marker:
        (home / "lgtm-host").touch()
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    if endpoint_override:
        monkeypatch.setitem(
            os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318"
        )
    else:
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp.observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is expected

    # The isolation verdict is frozen once per process, even if the marker
    # changes later; a restart is the apply boundary.
    if not marker and not endpoint_override:
        (home / "lgtm-host").touch()
        assert telemetry_otlp._OtlpBackend._enabled() is False


def test_registered_production_identity_with_lgtm_marker_enables_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "lgtm-host").touch()
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.setattr(telemetry_otlp, "production_identity", observability.production_identity)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_explicit_endpoint_override_allows_non_production_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: False)
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_pure_runner_export_relay_is_not_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    telemetry_otlp.observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is True


@pytest.mark.parametrize("exception_name", ["MachineRoleMissing", "MachineRoleInvalid"])
def test_unconfigured_machine_role_does_not_disable_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exception_name: str
) -> None:
    from base.cluster import machine

    exception_type = getattr(machine, exception_name)

    def missing_role() -> frozenset[str]:
        raise exception_type("role unavailable")

    monkeypatch.setattr(machine, "machine_role", missing_role)
    monkeypatch.setattr(machine, "machine_name", lambda: "macmini")
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava")
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    telemetry_otlp.observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is True
