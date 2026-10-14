"""Gateway probe and machine registration commands; split from tests/components/cli/test_commands.py (task #4554)."""

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

# ─── probe gateway via HTTP, not relying on pidfile ───────────────────────────────────


def _spec_by_service(service: str) -> _service_spec.ServiceSpec:
    """Look up a ServiceSpec by bare service name."""
    for spec in _roster.build_services():
        if spec.session == service:
            return spec
    raise AssertionError(f"no ServiceSpec service={service!r}")


def test_gateway_spec_uses_http_probe_not_pidfile() -> None:
    """Gateway uvicorn(reload=True) makes pidfile-based liveness unreliable — must probe HTTP.

    The gateway carries an identity-bound HTTP probe. A bare responding port or
    reload supervisor PID cannot certify that this unit owns the endpoint.
    """
    spec = _spec_by_service("gateway")
    assert spec.curl_url is not None, "gateway must use curl probe (uvicorn reload)"
    assert spec.curl_url.startswith("http://"), f"curl_url shape wrong: {spec.curl_url!r}"


def test_probe_gateway_takes_the_identity_path() -> None:
    """`probe_service(gateway_spec)` asks the identity probe, not a bare curl.

    The gateway declares one (`probe_home` — 2xx AND this unit's `$AVA_HOME`), so
    the operator surface and the watchdog ask the same question of the same port.
    A plain 2xx would still be satisfied by another cluster's gateway."""

    spec = _spec_by_service("gateway")
    from base.daemon.health import DaemonProbe

    spec = replace(spec, identity_probe=lambda: DaemonProbe.up("root-owned gateway"))
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is True
    assert probe.label == "identity"


def test_probe_gateway_reports_which_fact_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ✗ carries the reason. "down" and "answering, but it is another cluster's
    home" call for completely different actions, and this row is where an operator
    learns which one they have."""

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
    """A plugin's non-total `identity_probe` reports ✗ — it does not take `ava status`
    down with it.

    The three built-in probes convert every failure mode into a `DaemonProbe`;
    a plugin-registered one is under no such obligation. `ava status` is what an
    operator runs when the unit is ALREADY misbehaving, so one plugin raising must
    cost that plugin's row and nothing else."""
    import dataclasses

    def _boom() -> object:
        raise RuntimeError("no socket for you")

    spec = dataclasses.replace(_spec_by_service("gateway"), identity_probe=_boom)
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is None
    assert probe.label == "unavailable"
    # The type AND the message: "the probe is broken" and "the daemon is down" are
    # different problems, and a fixed string would have made them look alike.
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
    """gateway registration no longer requires AVA_GATEWAY_URL.

    WP4 (docs/conventions/data/reachability-and-credentials.md): the advertised URL is
    built on `reachable_host()` — the machine's private-network address — not
    on the bare gateway URL, so a gateway unit with a reachable identity
    registers a dialable address even before `AVA_GATEWAY_URL` is configured
    (a loopback gateway_url advertisement was what made the page proxy refuse
    the host's page servers, the 2026-08-30 serve 400). The port falls back to
    the gateway bind-port setting.
    """

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
    """A gateway-only unit advertises `reachable_host` + the gateway URL's port —
    NOT the bare gateway URL (WP4: the hostname is what the page proxy's SSRF
    allowlist consumes; a loopback advertisement breaks page serves)."""

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
    """An agent-runner registers its reachable ops server URL — the exact string the
    gateway later dials. Shape: http://<reachable-host>:<ops_port>."""
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
    """A remote agent-runner whose reachable address resolves to loopback must fail
    loud (exit 1): register_self raises LoopbackDialUrlRefused rather than writing a
    self-dialing localhost ops URL that a remote gateway would dial itself."""
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
    # register_self was reached with the loopback URL and rejected it; the caller
    # translated that into a non-zero exit rather than a persisted dead row.
    assert calls == ["http://127.0.0.1:8106"]


def _named_commands(text: str) -> set[str]:
    """The `ava ...` commands an operator hint names. The parser tests pin that each parses."""
    return set(re.findall(r"`(ava [^`]+)`", text))


# ─── remediation hints name commands that exist ──────────────────────────────


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
