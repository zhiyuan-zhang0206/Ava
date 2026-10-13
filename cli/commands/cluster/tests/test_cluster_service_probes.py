"""Service probes preserve roster gates and the caller's explicit machine."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import base.deploy.lifecycle.service_selection as lifecycle_service_selection
import services.supervision.roster.spec as roster_spec
from base.cluster.machine import MachineIdentity
from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.health_probe_inputs import (
    config_boot_environment as config_boot_environment,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_healthy as provider_guard_healthy,
)
from cli.commands.cluster.tests.health_probe_inputs import ran as ran
from cli.commands.cluster.tests.health_probe_inputs import signals as signals
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime


def test_service_probes_skips_gated_but_rejects_unknown_specs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Every intended service needs positive evidence; disabled services do not."""

    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    from services.supervision.roster.service_spec import ServiceSpec

    # requires_db is irrelevant here (no watchdog round involved), so it is uniform.
    def _spec(session: str) -> ServiceSpec:
        return ServiceSpec(
            session=session, cmd="x", capabilities=frozenset({"gateway"}), requires_db=True
        )

    dead = _spec("frontend")
    alive = _spec("gateway")
    gated = _spec("browser")
    probeless = _spec("browser-mcp")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def fake_services_for_capabilities_annotated(
        _roles: object, *, machine: MachineIdentity
    ) -> tuple[tuple[ServiceSpec, str | None], ...]:
        assert isinstance(machine, MachineIdentity)
        return (  # pyright: ignore[reportUnknownArgumentType]
            (dead, None),
            (alive, None),
            (gated, "disabled (AVA_BROWSER_ENABLED off)"),
            (probeless, None),
        )

    monkeypatch.setattr(
        roster_spec,
        "services_for_capabilities_annotated",
        fake_services_for_capabilities_annotated,
    )

    def _probe(spec: ServiceSpec) -> _probe_commands.ServiceProbe:
        alive = {"frontend": False, "gateway": True, "browser-mcp": None}[spec.session]
        return _probe_commands.ServiceProbe(alive, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _probe)

    assert cluster_health._service_probes(machine=command_runtime.machine) == [
        "frontend",
        "browser-mcp",
    ]


def test_service_probes_skips_gated_otel_collector_on_non_lgtm_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import services.supervision.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def fake_gateway_observability_home(_machine: MachineIdentity, *, home: Path) -> Path:
        assert isinstance(home, Path)
        assert isinstance(_machine, MachineIdentity)
        return tmp_home

    monkeypatch.setattr(
        roster_spec,
        "gateway_observability_home",
        fake_gateway_observability_home,
    )

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes(machine=command_runtime.machine) == []
    assert "otel-collector" not in recorded_sessions


def test_service_probes_checks_otel_collector_on_non_lgtm_gateway_with_explicit_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import services.supervision.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def fake_gateway_observability_home(_machine: MachineIdentity, *, home: Path) -> Path:
        assert isinstance(home, Path)
        assert isinstance(_machine, MachineIdentity)
        return tmp_home

    monkeypatch.setattr(
        roster_spec,
        "gateway_observability_home",
        fake_gateway_observability_home,
    )

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes(machine=command_runtime.machine) == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_checks_otel_collector_on_lgtm_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import services.supervision.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    (tmp_home / "lgtm-host").touch()
    recorded_sessions: list[str] = []

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def fake_gateway_observability_home(_machine: MachineIdentity, *, home: Path) -> Path:
        assert isinstance(home, Path)
        assert isinstance(_machine, MachineIdentity)
        return tmp_home

    monkeypatch.setattr(
        roster_spec,
        "gateway_observability_home",
        fake_gateway_observability_home,
    )

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes(machine=command_runtime.machine) == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_carry_the_failing_fact(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """A wrong-home listener must be diagnosed distinctly from an absent listener."""

    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    from services.supervision.roster.service_spec import ServiceSpec

    spec = ServiceSpec(
        session="ops", cmd="x", capabilities=frozenset({"gateway"}), requires_db=True
    )
    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def fake_services_for_capabilities_annotated(
        _roles: object, *, machine: MachineIdentity
    ) -> tuple[tuple[ServiceSpec, str | None], ...]:
        assert isinstance(machine, MachineIdentity)
        return ((spec, None),)

    monkeypatch.setattr(
        roster_spec,
        "services_for_capabilities_annotated",
        fake_services_for_capabilities_annotated,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        _probe_commands,
        "probe_service",
        lambda _spec: _probe_commands.ServiceProbe(
            False, "identity", "home='/home/ava/.ava' != '/u/.ava'"
        ),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert cluster_health._service_probes(machine=command_runtime.machine) == [
        "ops (home='/home/ava/.ava' != '/u/.ava')"
    ]


def test_service_probes_unknown_roster_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """A responding gateway cannot replace the missing service inventory."""

    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return None

    import cli.commands._repo as _repo_commands

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)
    assert cluster_health._service_probes(machine=command_runtime.machine) == [
        "service roster unavailable"
    ]


@pytest.mark.parametrize("mode,names", [("only", ["gateway"]), ("except", ["frontend"])])
def test_service_probes_respect_durable_service_intent(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    names: list[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import services.supervision.roster.service_spec as _service_spec
    from base.deploy.lifecycle import service_selection

    wanted = _service_spec.ServiceSpec("gateway", "unused", frozenset({"gateway"}), True)
    excluded = _service_spec.ServiceSpec("frontend", "unused", frozenset({"gateway"}), False)

    def roster(
        _roles: object, *, machine: MachineIdentity
    ) -> tuple[tuple[_service_spec.ServiceSpec, None], ...]:
        assert machine is command_runtime.machine
        return ((wanted, None), (excluded, None))

    def probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        assert spec is wanted
        return _probe_commands.ServiceProbe(True, "owned", "ready")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)
    monkeypatch.setattr(roster_spec, "services_for_capabilities_annotated", roster)
    monkeypatch.setattr(_probe_commands, "probe_service", probe)
    monkeypatch.setattr(
        service_selection,
        "read_selection",
        lambda: service_selection.ServiceSelection(mode, frozenset(names)),
    )
    assert cluster_health._service_probes(machine=command_runtime.machine) == []


def test_service_probes_unreadable_selection_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands

    def unreadable() -> None:
        raise ValueError("corrupt selection")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)
    monkeypatch.setattr(lifecycle_service_selection, "read_selection", unreadable)
    assert cluster_health._service_probes(machine=command_runtime.machine) == [
        "service selection unavailable (corrupt selection)"
    ]
