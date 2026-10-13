"""Roster invariants for services.supervision.roster.spec — the single desired-state source.

Replaces the slice-1 cross-source equivalence test now that `_repo` re-exports
`services.supervision.roster.spec` (there is one definition, not two to compare). These lock the roster's
capability semantics + the load-bearing ordering, and assert `_repo` is a pure
re-export façade so the "single source" property can't silently regress.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineIdentity, MachineRole
from base.daemon.endpoints import ServiceEndpoints
from services.supervision import roster
from services.supervision.roster import service_spec, spec
from tests.fixtures.unit.identity import machine_identity as machine_identity

_GATEWAY_SESSIONS = {
    "gate",
    "loki",
    "prometheus",
    "grafana",
    "gateway",
    "im-bridge",
    "labeler",
    "heartbeat",
    "delivery-watchdog",
    "events-maintenance",
    "task-maintenance",
    "memory-search",
    "memory-indexer",
    "frontend",
    "otel-collector",
    "pty-sessions",
    "pg-backup",
    "redis-bridge",
    "ttl-reaper",
    "schedule-manager",
    "insights",
}
_AGENT_RUNNER_SESSIONS = {
    "loki",
    "prometheus",
    "grafana",
    "page-server",
    "ops",
    "agent-host",
    "browser",
    "browser-mcp",
    "mcp-daemon",
    "computer-mcp",
    "otel-collector",
    "pty-sessions",
}


def test_agent_host_spec_launches_under_the_agent_profile() -> None:
    """The hosted agent-host daemon runs the agent kernel in-process, so its
    consumption matches the `agent` profile. The capabilities-derived marker
    (agent-runner-only -> "runner") would crash it at import (settings.agent
    read — 2026-08-30 soak startup); the spec must carry the explicit override
    so the root launcher's per-service environment
    (`root_driver._service_extra_env`) applies the "agent" profile marker to
    this session."""
    agent_host = next(s for s in roster.build_services() if s.session == "agent-host")
    assert agent_host.profile == "agent"
    assert service_spec.profile_marker(agent_host) == "agent"


def test_profile_override_wins_over_derivation_and_no_marker() -> None:
    """An explicit spec.profile beats the capabilities derivation and
    no_profile_marker alike (explicit beats derived, one rule)."""
    derived = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], frozenset({"agent-runner"})),
        requires_db=False,
    )
    assert service_spec.profile_marker(derived) == "runner"
    overridden = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], frozenset({"agent-runner"})),
        requires_db=False,
        profile="agent",
        no_profile_marker=True,
    )
    assert service_spec.profile_marker(overridden) == "agent"


def test_every_service_declares_non_empty_capabilities() -> None:
    for s in roster.build_services():
        assert s.capabilities, f"{s.session} declares no capabilities"
        assert s.capabilities <= {"gateway", "agent-runner", "observability-station"}


def test_capability_partition() -> None:
    """The `capabilities` field reproduces the intended gateway/agent-runner split
    (the encoding that replaced the exclusion set)."""
    by_session = {s.session: s for s in roster.build_services()}
    assert set(by_session) == _GATEWAY_SESSIONS | _AGENT_RUNNER_SESSIONS
    for session, s in by_session.items():
        on_gateway = "gateway" in s.capabilities
        on_runner = "agent-runner" in s.capabilities
        assert on_gateway == (session in _GATEWAY_SESSIONS)
        assert on_runner == (session in _AGENT_RUNNER_SESSIONS)


@pytest.mark.parametrize(
    ("role", "expected"),
    [("gateway", _GATEWAY_SESSIONS), ("agent-runner", _AGENT_RUNNER_SESSIONS)],
)
def test_annotated_roster_membership_per_capability(
    role: str, expected: set[str], *, machine_identity: MachineIdentity
) -> None:
    """The annotated (ungated) roster for a single capability is exactly that
    capability's services — env-independent, so gating does not perturb it."""
    got = {
        s.session
        for s, _reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine_identity
        )
    }
    assert got == expected


def test_station_roster_contains_only_lgtm_with_host_enablement_gate(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    def disabled(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return False

    def enabled(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return True

    monkeypatch.setattr("services.supervision.healthchecks.lgtm.is_lgtm_host", disabled)
    rows = spec.services_for_capabilities_annotated(
        frozenset({"observability-station"}), machine=machine_identity
    )
    assert {service.session for service, _reason in rows} == {"loki", "prometheus", "grafana"}
    assert all(reason is not None for _service, reason in rows)
    monkeypatch.setattr("services.supervision.healthchecks.lgtm.is_lgtm_host", enabled)
    assert {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"observability-station"}), machine=machine_identity
        )
    } == {"loki", "prometheus", "grafana"}


def test_gateway_roster_ordering_is_load_bearing() -> None:
    """memory-search must precede memory-indexer (memory-indexer cold-start connects to it)."""
    order = [s.session for s in roster.build_services()]
    assert order.index("memory-search") < order.index("memory-indexer")


def test_agent_runner_roster_ordering(*, machine_identity: MachineIdentity) -> None:
    """The agent host is available before the inbound ops service starts."""
    order = [
        s.session
        for s, _r in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=machine_identity
        )
    ]
    assert order.index("agent-host") < order.index("ops")


def test_application_roster_has_one_supervision_authority() -> None:
    services = {service.session: service for service in roster.build_services()}
    assert "gateway-watchdog" not in services
    assert "agent-runner-watchdog" not in services
    assert services["delivery-watchdog"].identity_probe is not None
    assert all("services.watchdog.daemon" not in service.cmd for service in services.values())


def test_browser_gated_when_disabled(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """The config gate flows through services.supervision.roster.spec: a disabled browser drops it from the
    start roster but keeps it (with a reason) in the annotated view."""
    monkeypatch.setattr(spec.settings.services, "browser_enabled", False)
    start = {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({"agent-runner"}), machine=machine_identity
        )
    }
    assert "browser" not in start
    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=machine_identity
        )
    }
    assert annotated["browser"] and "AVA_BROWSER_ENABLED" in annotated["browser"]
    assert annotated["browser-mcp"] and "AVA_BROWSER_ENABLED" in annotated["browser-mcp"]


def _agent_runner_annotated(
    monkeypatch: pytest.MonkeyPatch, *, local_machine: MachineIdentity
) -> dict[str, str | None]:
    monkeypatch.setattr(spec.settings.services, "browser_enabled", True)
    return {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=local_machine
        )
    }


def test_otel_collector_gated_on_gateway_without_lgtm_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)

    def observability_home(machine: MachineIdentity, *, home: Path) -> Path:
        assert machine is machine_identity
        return target_home

    target_home = home
    monkeypatch.setattr(spec, "gateway_observability_home", observability_home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    reason = annotated["otel-collector"]
    assert reason is not None
    assert "lgtm-host" in reason

    start = {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert "otel-collector" not in start


def test_otel_collector_runs_on_gateway_with_lgtm_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    (home / "lgtm-host").touch()

    def observability_home(machine: MachineIdentity, *, home: Path) -> Path:
        assert machine is machine_identity
        return target_home

    target_home = home
    monkeypatch.setattr(spec, "gateway_observability_home", observability_home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert annotated["otel-collector"] is None
    start = {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert "otel-collector" in start


def test_otel_collector_runs_on_gateway_without_marker_when_endpoint_override_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """An explicit AVA_TELEMETRY_OTLP_ENDPOINT opens the collector gate on a
    non-LGTM gateway — the same escape hatch the exporter side honors, so the
    roster keeps the collector for an operator who opted into explicit export."""
    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")

    def observability_home(machine: MachineIdentity, *, home: Path) -> Path:
        assert machine is machine_identity
        return target_home

    target_home = home
    monkeypatch.setattr(spec, "gateway_observability_home", observability_home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert annotated["otel-collector"] is None
    start = {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert "otel-collector" in start


def test_otel_collector_runs_on_pure_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    home = tmp_path / "runner"
    home.mkdir()

    def observability_home(machine: MachineIdentity, *, home: Path) -> None:
        assert machine is machine_identity

    monkeypatch.setattr(spec, "gateway_observability_home", observability_home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(
            frozenset({"agent-runner"}), machine=machine_identity
        )
    }
    assert annotated["otel-collector"] is None
    start = {
        service.session
        for service in spec.services_for_capabilities(
            frozenset({"agent-runner"}), machine=machine_identity
        )
    }
    assert "otel-collector" in start


def test_browser_mcp_gated_out_when_browser_is(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """An incapable browser host also drops browser-mcp, with the browser reason."""
    monkeypatch.setattr(
        "base.host.system.probes.browser_incapability", lambda: "no display (headless)"
    )
    monkeypatch.setattr(
        "services.supervision.roster.spec.browser_incapability", lambda: "no display (headless)"
    )
    annotated = _agent_runner_annotated(monkeypatch, local_machine=machine_identity)
    assert annotated["browser"] == "no display (headless)"
    assert annotated["browser-mcp"] == "no display (headless)"


# ─── the identity contract, stated on the roster ─────────────────────────
#
# `ava status` / `ava cluster health-probe` / the start readiness gate all take
# their verdict from `ServiceSpec.identity_probe`, so what the roster declares IS
# what the operator is told. A new daemon added without one would silently
# reopen the gap these tests exist for: an occupant on this unit's port reading
# green on the surface a human runs.


def test_every_service_declares_an_identity_probe() -> None:
    """Every health verdict must bind the responder to this root-owned service."""
    missing = {s.session for s in roster.build_services() if s.identity_probe is None}
    assert missing == set(), f"{sorted(missing)} have no identity-bound health probe"


def test_daemon_identity_binds_the_probe_to_one_daemons_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`daemon_identity` is what plugin-registered services reuse, so its shape is
    part of the roster's public contract: the daemon name, this cluster's
    `/healthz` URL (resolved at build time like every other probe target) and this
    unit's pidfile, all three reaching `probe_daemon`."""
    seen: dict[str, object] = {}

    def _capture(name: str, url: str, *, pidfile: Path, **_kw: object) -> object:
        seen.update(name=name, url=url, pidfile=pidfile)
        from base.daemon.health import DaemonProbe

        return DaemonProbe.up("stub")

    monkeypatch.setattr("services.supervision.roster.healthz.probe_daemon", _capture)
    pidfile = tmp_path / "ops.pid"
    assert roster.daemon_identity("ops", pidfile)().alive is True
    assert seen["name"] == "ops"
    assert seen["pidfile"] == pidfile
    assert str(ServiceEndpoints.from_settings().of("ops").health_port) in str(seen["url"])


def test_im_bridge_gated_when_disabled(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """AVA_IM_BRIDGE_ENABLED=false gates im-bridge out of the roster — the
    no-adapter daemon otherwise exits immediately and the watchdog fails its
    healthcheck every round (2026-08-10 preview noise)."""
    from base.config import settings

    monkeypatch.setattr(settings.services, "im_bridge_enabled", False)
    annotated = {
        s.session: reason
        for s, reason in spec.services_for_capabilities_annotated(
            frozenset({"gateway"}), machine=machine_identity
        )
    }
    assert "im-bridge" in annotated
    assert annotated["im-bridge"] == "disabled (AVA_IM_BRIDGE_ENABLED off)"


# ── AVA_PROCESS_PROFILE marker derivation (task #1230) ──


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (frozenset({"gateway"}), "gateway"),
        (frozenset({"agent-runner"}), "runner"),
        # Both capabilities (a service that runs on either host) gets no marker:
        # the gateway pop must not run against a service that is not gateway-only.
        (frozenset({"gateway", "agent-runner"}), None),
        # No capabilities is no longer a spec that can exist: construction refuses it
        # (`services/supervision/roster/tests/test_healthz_daemon.py`).
    ],
)
def test_profile_marker_derives_from_capabilities(
    caps: frozenset[str], expected: str | None
) -> None:
    """profile_marker() maps a spec's capability set to the AVA_PROCESS_PROFILE
    value its session should carry — unless the spec opts out explicitly."""
    s = service_spec.ServiceSpec(
        session="x",
        cmd="true",
        capabilities=cast(frozenset[MachineRole], caps),
        requires_db=False,
    )
    assert service_spec.profile_marker(s) == expected


def test_labeler_is_the_only_service_that_opts_out_of_the_profile_marker() -> None:
    """The labeler builds chat models (it generates labels) and therefore
    consumes the agent-runner-capability LLM provider keys (DEEPSEEK_API_KEY
    among them). The gateway profile's env-authority pop would remove them from
    its os.environ at boot, so its spec must boot profile-less — and EVERY
    other service must keep its marker so the pop still keeps provider keys out
    of their processes (task #1230: fix the labeler only, do not scatter keys
    into all gateway-profile daemons)."""
    by_session = {s.session: s for s in roster.build_services()}
    opted_out = {sess for sess, s in by_session.items() if s.no_profile_marker}
    assert opted_out == {"labeler"}, (
        f"expected only the labeler to opt out of the profile marker, got {opted_out}"
    )
    assert service_spec.profile_marker(by_session["labeler"]) is None
    for sess, s in by_session.items():
        if sess == "labeler":
            continue
        # A both-capability service (otel-collector) cannot carry one marker —
        # it runs on both roles, and the derivation is "both -> neutral". Its
        # Go binary never reads provider keys, so the neutral env is safe.
        assert service_spec.profile_marker(s) is not None or (
            "agent-runner" in s.capabilities and "gateway" in s.capabilities
        ), (
            f"{sess} must keep its AVA_PROCESS_PROFILE marker (provider keys stay "
            "out of its process)"
        )


def test_gateway_daemons_keep_the_gateway_marker() -> None:
    """The sibling gateway daemons (heartbeat / events-maintenance / im-bridge /
    memory-indexer / ...) still boot as gateway-profile processes — their envs
    must not gain the provider keys the labeler needs (anti-spread)."""
    by_session = {s.session: s for s in roster.build_services()}
    for sess in ("heartbeat", "events-maintenance", "im-bridge", "memory-indexer", "gateway"):
        assert service_spec.profile_marker(by_session[sess]) == "gateway", sess


def test_runner_daemons_keep_the_runner_marker() -> None:
    by_session = {s.session: s for s in roster.build_services()}
    for sess in ("ops", "page-server"):
        assert service_spec.profile_marker(by_session[sess]) == "runner", sess
