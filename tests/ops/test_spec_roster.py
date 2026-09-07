"""Roster invariants for ops.spec — the single desired-state source.

Replaces the slice-1 cross-source equivalence test now that `_repo` re-exports
`ops.spec` (there is one definition, not two to compare). These lock the roster's
capability semantics + the load-bearing ordering, and assert `_repo` is a pure
re-export façade so the "single source" property can't silently regress.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from cli.commands import _repo
from ops import roster, service_spec, spec
from shared.machine import MachineRole

_GATEWAY_SESSIONS = {
    "gateway",
    "im-bridge",
    "labeler",
    "heartbeat",
    "delivery-watchdog",
    "events-maintenance",
    "task-maintenance",
    "milvus",
    "memory-search",
    "memory-indexer",
    "frontend",
    "gateway-watchdog",
    "otel-collector",
    "pg-backup",
    "pitr-uploader",
    "pitr-base-candidate",
}
_AGENT_RUNNER_SESSIONS = {
    "page-server",
    "agent-runner-watchdog",
    "ops",
    "agent-host",
    "browser",
    "browser-mcp",
    "mcp-daemon",
    "computer-mcp",
    "otel-collector",
}


def test_repo_is_a_pure_reexport_of_ops_spec() -> None:
    """`_repo`'s roster names are the SAME objects as ops.spec — proving the
    definitions live once (single source), not duplicated."""
    assert _repo.build_services is roster.build_services
    assert _repo.ServiceSpec is service_spec.ServiceSpec
    assert _repo.profile_marker is service_spec.profile_marker
    assert _repo._services_for_roles is spec.services_for_capabilities
    assert _repo._services_for_roles_annotated is spec.services_for_capabilities_annotated


def test_agent_host_spec_launches_under_the_agent_profile() -> None:
    """The hosted agent-host daemon runs the agent kernel in-process, so its
    consumption matches the `agent` profile. The capabilities-derived marker
    (agent-runner-only -> "runner") would crash it at import (settings.agent
    read — 2026-08-30 soak startup); the spec must carry the explicit override
    so BOTH launch paths (ava start / ava restart spec path AND the watchdog
    respawn path) agree."""
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
        assert s.capabilities <= {"gateway", "agent-runner"}


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
def test_annotated_roster_membership_per_capability(role: str, expected: set[str]) -> None:
    """The annotated (ungated) roster for a single capability is exactly that
    capability's services — env-independent, so gating does not perturb it."""
    got = {s.session for s, _reason in spec.services_for_capabilities_annotated(frozenset({role}))}
    assert got == expected


def test_pure_station_runs_no_session_services() -> None:
    """A pure observability-station host runs NO session service: every service
    spec is gated on gateway and/or agent-runner, so `ava start` on a station
    unit only converges the native LGTM backends. This is what makes
    register_self (via `ava start`) the sole writer of a station unit's
    machine_units row — the ops daemon's boot registration never runs there."""
    assert spec.services_for_capabilities(frozenset({"observability-station"})) == ()


def test_gateway_roster_ordering_is_load_bearing() -> None:
    """milvus must precede memory-indexer (memory-indexer cold-start connects to it).

    Uses the ungated declaration order (``build_services``): the invariant is
    the load-bearing spec sequence. The start roster drops milvus entirely
    under the numpy backend (see test_milvus_gated_out_unless_milvus_backend),
    so it cannot carry an ordering check.
    """
    order = [s.session for s in roster.build_services()]
    assert order.index("milvus") < order.index("memory-indexer")


def test_agent_runner_roster_ordering() -> None:
    """The agent host is available before the inbound ops service starts."""
    order = [
        s.session for s, _r in spec.services_for_capabilities_annotated(frozenset({"agent-runner"}))
    ]
    assert order.index("agent-host") < order.index("ops")


def test_watchdogs_declare_no_healthcheck_module() -> None:
    # Keyed off the cmd module, not the session-name suffix: delivery-watchdog
    # is a monitored service whose session ends in "watchdog" but runs its own
    # module with its own healthcheck.
    watchdogs = [s for s in roster.build_services() if "services.watchdog.daemon" in s.cmd]
    assert watchdogs
    assert all(s.healthcheck_module is None for s in watchdogs)


def test_browser_gated_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config gate flows through ops.spec: a disabled browser drops it from the
    start roster but keeps it (with a reason) in the annotated view."""
    monkeypatch.setattr(spec.settings.services, "browser_enabled", False)
    start = {s.session for s in spec.services_for_capabilities(frozenset({"agent-runner"}))}
    assert "browser" not in start
    annotated = {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(frozenset({"agent-runner"}))
    }
    assert annotated["browser"] and "AVA_BROWSER_ENABLED" in annotated["browser"]
    assert annotated["browser-mcp"] and "AVA_BROWSER_ENABLED" in annotated["browser-mcp"]


def test_base_candidate_is_independently_gated_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spec.settings.physical_backup, "pitr_enabled", True)
    monkeypatch.setattr(spec.settings.physical_backup, "pitr_base_backup_enabled", False)
    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    assert "BASE_BACKUP_ENABLED" in (annotated["pitr-base-candidate"] or "")
    start = {service.session for service in spec.services_for_capabilities(frozenset({"gateway"}))}
    assert "pitr-base-candidate" not in start


def _agent_runner_annotated(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    monkeypatch.setattr(spec.settings.services, "browser_enabled", True)
    return {
        s.session: r
        for s, r in spec.services_for_capabilities_annotated(frozenset({"agent-runner"}))
    }


def test_otel_collector_gated_on_gateway_without_lgtm_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(spec, "gateway_observability_home", lambda: home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    reason = annotated["otel-collector"]
    assert reason is not None
    assert "lgtm-host" in reason

    start = {service.session for service in spec.services_for_capabilities(frozenset({"gateway"}))}
    assert "otel-collector" not in start


def test_otel_collector_runs_on_gateway_with_lgtm_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    (home / "lgtm-host").touch()
    monkeypatch.setattr(spec, "gateway_observability_home", lambda: home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    assert annotated["otel-collector"] is None
    start = {service.session for service in spec.services_for_capabilities(frozenset({"gateway"}))}
    assert "otel-collector" in start


def test_otel_collector_runs_on_gateway_without_marker_when_endpoint_override_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit AVA_TELEMETRY_OTLP_ENDPOINT opens the collector gate on a
    non-LGTM gateway — the same escape hatch the exporter side honors, so the
    roster keeps the collector for an operator who opted into explicit export."""
    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(spec, "gateway_observability_home", lambda: home)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    assert annotated["otel-collector"] is None
    start = {service.session for service in spec.services_for_capabilities(frozenset({"gateway"}))}
    assert "otel-collector" in start


def test_otel_collector_runs_on_pure_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "runner"
    home.mkdir()
    monkeypatch.setattr(spec, "gateway_observability_home", lambda: None)

    annotated = {
        service.session: reason
        for service, reason in spec.services_for_capabilities_annotated(frozenset({"agent-runner"}))
    }
    assert annotated["otel-collector"] is None
    start = {
        service.session for service in spec.services_for_capabilities(frozenset({"agent-runner"}))
    }
    assert "otel-collector" in start


def test_browser_mcp_gated_out_without_af_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with display + Chrome + npx but no AF_UNIX (a Windows agent-runner)
    keeps `browser` in the start roster and drops `browser-mcp` WITH a reason.

    Before the split gate both services shared `browser_incapability()`, so
    browser-mcp was unconditionally in a Windows runner's roster, failed every
    launch, and `ava status` showed no skip annotation to say why."""
    monkeypatch.setattr("shared.platform_probes.unix_sockets_available", lambda: False)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    annotated = _agent_runner_annotated(monkeypatch)
    assert annotated["browser"] is None
    assert annotated["browser-mcp"] is not None
    assert "AF_UNIX" in annotated["browser-mcp"]
    start = {s.session for s in spec.services_for_capabilities(frozenset({"agent-runner"}))}
    assert "browser" in start
    assert "browser-mcp" not in start


def test_browser_mcp_ungated_with_af_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a capable POSIX host both services start — the AF_UNIX prong is the
    only thing the two gates differ by."""
    monkeypatch.setattr("shared.platform_probes.unix_sockets_available", lambda: True)
    monkeypatch.setattr("shared.platform_probes.browser_incapability", lambda: None)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    annotated = _agent_runner_annotated(monkeypatch)
    assert annotated["browser"] is None
    assert annotated["browser-mcp"] is None


def test_browser_mcp_gated_out_when_browser_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """browser-mcp's gate is a SUPERSET: an incapable browser host also drops
    browser-mcp, with the browser reason (not the AF_UNIX one)."""
    monkeypatch.setattr("shared.platform_probes.unix_sockets_available", lambda: True)
    monkeypatch.setattr(
        "shared.platform_probes.browser_incapability", lambda: "no display (headless)"
    )
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")
    annotated = _agent_runner_annotated(monkeypatch)
    assert annotated["browser"] == "no display (headless)"
    assert annotated["browser-mcp"] == "no display (headless)"


def test_mcp_daemon_gated_out_without_af_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp-daemon's transport is a Unix socket, so a host without AF_UNIX (a
    Windows agent-runner) must not have it in the start roster — otherwise the
    daemon fails every launch AND the watchdog judges it dead every 60s and
    logs a restart failure (the pre-fix win runner: 1,257 ERROR/24h)."""
    monkeypatch.setattr("ops.spec.unix_sockets_available", lambda: False)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    annotated = _agent_runner_annotated(monkeypatch)
    assert annotated["mcp-daemon"] is not None
    assert "AF_UNIX" in annotated["mcp-daemon"]
    start = {s.session for s in spec.services_for_capabilities(frozenset({"agent-runner"}))}
    assert "mcp-daemon" not in start


def test_mcp_daemon_ungated_with_af_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a capable POSIX host mcp-daemon stays in the start roster — the
    AF_UNIX prong is the only thing its gate checks."""
    monkeypatch.setattr("ops.spec.unix_sockets_available", lambda: True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    annotated = _agent_runner_annotated(monkeypatch)
    assert annotated["mcp-daemon"] is None
    start = {s.session for s in spec.services_for_capabilities(frozenset({"agent-runner"}))}
    assert "mcp-daemon" in start


# ─── the identity contract, stated on the roster ─────────────────────────
#
# `ava status` / `ava cluster health-probe` / the start readiness gate all take
# their verdict from `ServiceSpec.identity_probe`, so what the roster declares IS
# what the operator is told. A new daemon added without one would silently
# reopen the gap these tests exist for: an occupant on this unit's port reading
# green on the surface a human runs.

# The services whose endpoint genuinely cannot prove who answered. Enumerated,
# not derived, so adding a service forces a decision instead of defaulting into
# liveness-only.
_LIVENESS_ONLY_SESSIONS = {
    "frontend",  # Next.js — serves no Ava payload; a 2xx is all there is
    "milvus",  # gRPC — TCP connect only, uncurlable
    "memory-search",  # the search API carries no Ava identity payload; its healthcheck traverses a real /search
    "browser-mcp",  # MCP over a Unix socket its own healthcheck dials
    "computer-mcp",  # MCP over a Unix socket; its healthcheck dials + pings it
    "mcp-daemon",  # MCP over a Unix socket; its healthcheck dials + pings it
    "gateway-watchdog",  # not a server; its pidfile is the whole signal
    "agent-runner-watchdog",
}


def test_every_healthz_service_declares_an_identity_probe() -> None:
    """Anything that serves an endpoint capable of identifying itself must say so
    on the roster — otherwise the operator surface falls back to a bare 2xx,
    which is exactly what an impostor satisfies."""
    missing = {
        s.session
        for s in roster.build_services()
        if s.identity_probe is None and s.session not in _LIVENESS_ONLY_SESSIONS
    }
    assert missing == set(), (
        f"{sorted(missing)} have no identity_probe and are not declared liveness-only; "
        "either give them one or add them to _LIVENESS_ONLY_SESSIONS with the reason"
    )


def test_liveness_only_services_declare_no_identity_probe() -> None:
    """The other direction: the exemption list cannot go stale silently either."""
    wrong = {
        s.session
        for s in roster.build_services()
        if s.identity_probe is not None and s.session in _LIVENESS_ONLY_SESSIONS
    }
    assert wrong == set(), f"{sorted(wrong)} gained an identity probe — drop them from the list"


def test_browser_identity_is_the_profile_probe_not_a_curl() -> None:
    """The browser's `curl_url` stays (the CDP endpoint is still what gets
    dialled), but the verdict comes from the profile-anchored probe — CDP itself
    carries no field we control, so a 200 there says nothing about whose Chrome
    answered."""
    from services.browser.probe import probe_browser

    browser = next(s for s in roster.build_services() if s.session == "browser")
    assert browser.identity_probe is roster._browser_probe
    assert probe_browser is not None  # the lazy import target exists


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
        from shared.daemon_health import DaemonProbe

        return DaemonProbe.up("stub")

    monkeypatch.setattr("shared.daemon_health._probe_daemon", _capture)
    pidfile = tmp_path / "ops.pid"
    assert roster.daemon_identity("ops", pidfile)().alive is True
    assert seen["name"] == "ops"
    assert seen["pidfile"] == pidfile
    assert str(roster.health_port("ops")) in str(seen["url"])


def test_im_bridge_gated_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_IM_BRIDGE_ENABLED=false gates im-bridge out of the roster — the
    no-adapter daemon otherwise exits immediately and the watchdog fails its
    healthcheck every round (2026-08-10 preview noise)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "im_bridge_enabled", False)
    annotated = {
        s.session: reason
        for s, reason in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    assert "im-bridge" in annotated
    assert annotated["im-bridge"] == "disabled (AVA_IM_BRIDGE_ENABLED off)"


def test_milvus_gated_out_unless_milvus_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The milvus-lite server gates on the memory-search backend: numpy
    (default) and pgvector never dial it, so the ~1GB daemon must not be in
    the start roster (2026-09-02 numpy-default ruling, task #2347)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "numpy")
    reason = spec.gate_reason_for_session("milvus")
    assert reason is not None
    assert "milvus not needed" in reason

    monkeypatch.setattr(settings.services, "memory_search_backend", "milvus")
    assert spec.gate_reason_for_session("milvus") is None


# ── AVA_PROCESS_PROFILE marker derivation (task #1230) ──


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (frozenset({"gateway"}), "gateway"),
        (frozenset({"agent-runner"}), "runner"),
        # Both capabilities (a service that runs on either host) gets no marker:
        # the gateway pop must not run against a service that is not gateway-only.
        (frozenset({"gateway", "agent-runner"}), None),
        (frozenset[MachineRole](), None),
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


def test_gate_reason_for_session_matches_the_start_roster_decision() -> None:
    """The single-service gate agrees with the roster and refuses retired services."""
    assert spec.gate_reason_for_session("agent-host") is None
    assert "agent-host" in {
        s.session for s in spec.services_for_capabilities(frozenset({"agent-runner"}))
    }
    assert "restarter" not in {s.session for s in spec.build_services()}
    assert spec.gate_reason_for_session("restarter") is not None
    assert spec.gate_reason_for_session("no-such-service") is not None
