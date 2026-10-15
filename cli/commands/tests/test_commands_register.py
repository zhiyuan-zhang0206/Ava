from __future__ import annotations

import re
from dataclasses import replace
from typing import cast

import pytest

import cli.commands.probe as _probe_commands
import services.supervision.roster as _roster
import services.supervision.roster.service_spec as _service_spec
from base.cluster import machines
from base.cluster.machine import MachineRoles
from base.daemon.tests.fakes import pin_endpoints
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.schema import migrations
from cli.commands._repo import register_machine_or_die
from cli.commands._setup import SetupValues
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_fakes import fake_session_backends as fake_session_backends
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _spec_by_service(service: str) -> _service_spec.ServiceSpec:
    for spec in _roster.build_services():
        if spec.session == service:
            return spec
    raise AssertionError(f"no ServiceSpec service={service!r}")


def test_gateway_spec_uses_http_probe_not_pidfile() -> None:
    spec = _spec_by_service("gateway")
    assert spec.curl_url is not None, "gateway must use curl probe (uvicorn reload)"
    assert spec.curl_url.startswith("http://"), f"curl_url shape wrong: {spec.curl_url!r}"


def test_probe_gateway_takes_the_identity_path() -> None:

    spec = _spec_by_service("gateway")
    from base.daemon.health import DaemonProbe

    spec = replace(spec, identity_probe=lambda: DaemonProbe.up("root-owned gateway"))
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is True
    assert probe.label == "identity"


def test_probe_gateway_reports_which_fact_failed(monkeypatch: pytest.MonkeyPatch) -> None:

    spec = _spec_by_service("gateway")
    from base.daemon.health import DaemonProbe

    spec = replace(
        spec,
        identity_probe=lambda: DaemonProbe.port_taken("identity mismatch: home='/home/ava/.ava'"),
    )
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is False
    assert "/home/ava/.ava" in probe.detail


def test_probe_survives_an_identity_probe_that_raises() -> None:
    import dataclasses

    def _boom() -> object:
        raise RuntimeError("no socket for you")

    spec = dataclasses.replace(_spec_by_service("gateway"), identity_probe=_boom)
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is None
    assert probe.label == "unavailable"
    assert "RuntimeError" in probe.detail
    assert "no socket for you" in probe.detail


def test_service_without_identity_probe_cannot_claim_readiness() -> None:
    spec = replace(_spec_by_service("gateway"), identity_probe=None)
    result = _probe_commands.probe_service(spec)
    assert result.alive is None
    assert result.label == "unavailable"


def test_register_gateway_advertises_without_gateway_url(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    command_runtime: CommandRuntime,
) -> None:

    calls: list[str | None] = []

    def fake_register_self(
        _db: object,
        *,
        url: str | None = None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert name == command_runtime.machine.name()
        assert roles and home
        assert description == command_runtime.machine.description()
        assert gateway == command_runtime.config.view.gateway.gateway_url
        calls.append(url)

    monkeypatch.setattr(machines, "register_self", fake_register_self)
    command_runtime.config.set_field("gateway_url", "")
    command_runtime.config.set_field("gateway_port", 8000)
    command_runtime.machine.set(host="10.0.0.2")

    rc = register_machine_or_die(
        Database.from_settings(gate=database_gate),
        cast(SetupValues, {"machine_name": "control"}),
        frozenset({"gateway"}),
        command=command_runtime,
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_gateway_only_advertises_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    command_runtime: CommandRuntime,
) -> None:

    calls: list[str | None] = []

    def fake_register_self(
        _db: object,
        *,
        url: str | None = None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert name == command_runtime.machine.name()
        assert roles and home
        assert description == command_runtime.machine.description()
        assert gateway == command_runtime.config.view.gateway.gateway_url
        calls.append(url)

    monkeypatch.setattr(machines, "register_self", fake_register_self)
    command_runtime.config.set_field("gateway_url", "https://ava.example:8000")
    command_runtime.machine.set(host="10.0.0.2")

    rc = register_machine_or_die(
        Database.from_settings(gate=database_gate),
        cast(SetupValues, {"machine_name": "control"}),
        frozenset({"gateway"}),
        command=command_runtime,
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_agent_runner_advertises_ops_url(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    command_runtime: CommandRuntime,
) -> None:
    calls: list[str | None] = []

    def fake_register_self(
        _db: object,
        *,
        url: str | None = None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert name == command_runtime.machine.name()
        assert roles and home
        assert description == command_runtime.machine.description()
        assert gateway == command_runtime.config.view.gateway.gateway_url
        calls.append(url)

    monkeypatch.setattr(machines, "register_self", fake_register_self)
    command_runtime.machine.set(host="10.0.0.2")
    pin_endpoints(monkeypatch, port=lambda name: 8106 if name == "ops" else 0)

    rc = register_machine_or_die(
        Database.from_settings(gate=database_gate),
        cast(SetupValues, {"machine_name": "runner-a"}),
        frozenset({"agent-runner"}),
        command=command_runtime,
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8106"]


def test_register_agent_runner_loopback_host_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    command_runtime: CommandRuntime,
) -> None:
    from base.cluster.machines import LoopbackDialUrlRefused

    calls: list[str | None] = []

    def _reject(
        _db: object,
        *,
        url: str | None = None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        assert name == command_runtime.machine.name()
        assert roles and home
        assert description == command_runtime.machine.description()
        assert gateway == command_runtime.config.view.gateway.gateway_url
        calls.append(url)
        raise LoopbackDialUrlRefused(f"loopback dial url refused: {url}")

    monkeypatch.setattr(machines, "register_self", _reject)
    command_runtime.machine.set(host="127.0.0.1")
    pin_endpoints(monkeypatch, port=lambda name: 8106 if name == "ops" else 0)

    rc = register_machine_or_die(
        Database.from_settings(gate=database_gate),
        cast(SetupValues, {"machine_name": "runner-a"}),
        frozenset({"agent-runner"}),
        command=command_runtime,
    )
    assert rc == 1
    assert calls == ["http://127.0.0.1:8106"]


def _named_commands(text: str) -> set[str]:
    return set(re.findall(r"`(ava [^`]+)`", text))


def test_register_schema_behind_hint_names_working_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    database_gate: ProcessDbGate,
    command_runtime: CommandRuntime,
) -> None:
    import psycopg

    def _missing_table(
        _db: object,
        *,
        url: str | None = None,
        name: str,
        roles: MachineRoles,
        description: str | None,
        home: str,
        gateway: str | None,
    ) -> None:
        del _db, url, name, roles, description, home, gateway
        raise psycopg.errors.UndefinedTable('relation "machines" does not exist')

    monkeypatch.setattr(machines, "register_self", _missing_table)
    command_runtime.machine.set(host="10.0.0.2")

    rc = register_machine_or_die(
        Database.from_settings(gate=database_gate),
        cast(SetupValues, {"machine_name": "gw"}),
        frozenset({"gateway"}),
        command=command_runtime,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "`ava stop` then `ava start` on the gateway" in err
    assert _named_commands(err) == {"ava stop", "ava start"}


def test_code_behind_schema_hint_names_working_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import cli.commands._repo as _repo_commands
    from base.deploy.schema.migrations import CodeBehindSchema

    def _ahead(_url: str) -> None:
        raise CodeBehindSchema("DB has migrations this checkout lacks")

    monkeypatch.setattr(migrations, "assert_schema_current", _ahead)

    assert _repo_commands.assert_schema_current_or_die("postgresql://ava@127.0.0.1:1/ava") == 1
    err = capsys.readouterr().err
    assert "gateway's revision" in err
    assert _named_commands(err) == {"ava start"}
