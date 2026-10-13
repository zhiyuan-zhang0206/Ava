"""OTLP eligibility is supplied by a concrete machine owner, never global identity."""

import os
from pathlib import Path

import pytest

from base.cluster import derive
from base.cluster.machine import MachineNameMissing, MachineRoleInvalid, MachineRoleMissing
from base.telemetry import observability
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.identity_inputs import machine_identity


def _production_label(_home: Path | None = None) -> str:
    return ".ava"


@pytest.mark.parametrize(
    ("name", "cluster", "expected"),
    [("runner", ".ava", True), ("runner", "other", False), ("", ".ava", False)],
)
def test_production_identity_requires_name_and_production_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, cluster: str, expected: bool
) -> None:
    def label(_home: Path) -> str:
        return cluster

    monkeypatch.setattr(derive, "home_label", label)
    assert derive.production_identity(machine_identity(name=name), home=tmp_path) is expected


@pytest.mark.parametrize(
    ("roles", "marker", "override", "expected"),
    [
        (frozenset({"agent-runner"}), False, False, True),
        (frozenset({"gateway"}), False, False, False),
        (frozenset({"gateway"}), True, False, True),
        (frozenset({"gateway"}), False, True, True),
        (frozenset({"gateway", "observability-station"}), False, False, True),
        (None, False, False, True),
    ],
)
def test_export_gate_roles_marker_and_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    roles: frozenset[str] | None,
    marker: bool,
    override: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    if override:
        monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://explicit:4318")
    if marker:
        (tmp_path / "lgtm-host").touch()
    assert (
        telemetry_otlp.observability_export_allowed(machine_identity(roles=roles), home=tmp_path)
        is expected
    )


def test_explicit_endpoint_does_not_read_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    machine = machine_identity()

    def reject() -> str:
        raise AssertionError("explicit endpoints need no production identity")

    monkeypatch.setattr(machine, "name", reject)
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://explicit:4318")
    assert telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


def test_missing_role_preserves_runner_bootstrap_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    machine = machine_identity()

    def missing() -> frozenset[str]:
        raise MachineRoleMissing("not configured")

    monkeypatch.setattr(machine, "role", missing)
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


@pytest.mark.parametrize("operation", ["gateway", "station", "export"])
def test_invalid_roles_propagate_outside_transport_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    machine = machine_identity(roles=frozenset({"unknown"}))
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    with pytest.raises(MachineRoleInvalid):
        if operation == "gateway":
            observability.gateway_observability_home(machine, home=tmp_path)
        elif operation == "station":
            observability.home_is_observability_station(
                tmp_path, machine=machine, unit_home=tmp_path
            )
        else:
            telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


def test_each_backend_freezes_only_its_own_eligibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    decision = False
    calls: list[bool] = []

    def read() -> bool:
        calls.append(decision)
        return decision

    first = telemetry_otlp.OtlpBackend(export_allowed=read)
    assert first._enabled() is False
    decision = True
    assert first._enabled() is False
    second = telemetry_otlp.OtlpBackend(export_allowed=read)
    assert second._enabled() is True
    assert calls == [False, True]


@pytest.mark.parametrize("error", [MachineRoleInvalid, RuntimeError])
def test_backend_does_not_cache_identity_defects_as_disabled(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    def invalid() -> bool:
        raise error("bad identity")

    backend = telemetry_otlp.OtlpBackend(export_allowed=invalid)
    with pytest.raises(error, match="bad identity"):
        backend.warmup()
    assert backend._export_decision is None
    assert backend._worker is None


def test_disabled_backend_does_not_read_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", False)

    def reject() -> bool:
        raise MachineNameMissing("identity remains cold")

    assert telemetry_otlp.OtlpBackend(export_allowed=reject)._enabled() is False
