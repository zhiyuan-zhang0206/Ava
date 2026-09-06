"""Status aggregation over the Spec roster — the probe/scheduling view and the
live liveness snapshot, without touching the healthcheck modules."""

from __future__ import annotations

import pytest

from ops import observe


def test_probe_set_gateway_classifies_signal_types() -> None:
    """The gateway probe set carries each service's probe signal + its watchdog
    module, derived from the roster (so it can't drift from what start launches)."""
    views = {v.session: v for v in observe.probe_set(frozenset({"gateway"}))}
    # The gateway's `/api/health` carries `home`, so it is probed for identity,
    # not liveness — a 2xx from another cluster's gateway is not this one being up.
    assert views["gateway"].kind == "identity"
    assert views["frontend"].kind == "http"  # Next.js — 2xx is all the endpoint has
    assert views["milvus"].kind == "tcp"  # gRPC — TCP-connect probe, not curl
    assert views["gateway-watchdog"].kind == "pid"  # pidfile only, no HTTP/TCP
    assert views["gateway"].healthcheck_module == "services.healthchecks.gateway"
    assert views["gateway-watchdog"].healthcheck_module is None  # the monitor itself


def test_probe_set_agent_runner_membership() -> None:
    """Every runner reports its single agent host and shared local services."""
    views = {v.session: v for v in observe.probe_set(frozenset({"agent-runner"}))}
    assert set(views) == {
        "ops",
        "page-server",
        "agent-runner-watchdog",
        "agent-host",
        "browser",
        "browser-mcp",
        "mcp-daemon",
        "computer-mcp",
        "otel-collector",
    }
    assert views["browser-mcp"].kind == "none"
    assert views["agent-host"].gate_reason is None
    assert views["agent-host"].kind == "identity"
    assert views["agent-host"].healthcheck_module == "services.healthchecks.agent_host"


def test_observe_gated_service_reports_na_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config-gated service (browser, disabled) is not probed — it reports
    alive=None + its gate reason, so Status shows why it is absent."""
    monkeypatch.setattr("shared.config.settings.services.browser_enabled", False)
    statuses = {s.session: s for s in observe.observe_services(frozenset({"agent-runner"}))}
    br = statuses["browser"]
    assert br.alive is None
    assert br.kind == "gated"
    assert br.gate_reason and "AVA_BROWSER_ENABLED" in br.gate_reason


def test_observe_runs_probe_for_active_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """An active liveness-only HTTP service is probed; a 2xx/3xx maps to alive=True."""
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(observe, "_curl_ok", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    # Nothing in a unit test may dial a real health endpoint — the identity probes
    # would reach whatever holds those ports on the machine running the suite.
    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.daemon_health._probe_daemon",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    statuses = {s.session: s for s in observe.observe_services(frozenset({"gateway"}))}
    assert statuses["frontend"].alive is True
    assert statuses["frontend"].kind == "http"


def test_observe_asks_identity_where_the_endpoint_carries_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service with an `identity_probe` never falls back to a bare 2xx: Status
    reports what the identity check concluded, so an occupant on this unit's port
    reads dead here exactly as it does to the watchdog."""
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(observe, "_curl_ok", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.daemon_health._probe_daemon",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.port_taken("another unit's daemon holds this port"),  # pyright: ignore[reportUnknownArgumentType]
    )
    statuses = {s.session: s for s in observe.observe_services(frozenset({"gateway"}))}
    assert statuses["gateway"].alive is False
    assert statuses["gateway"].kind == "identity"


def test_reported_kind_agrees_with_the_probe_actually_run() -> None:
    """The label and the check must never come apart. `_probe_kind_target` names the
    signal the row shows and `_run_probe` decides the verdict, off the same spec —
    if the classifier fell through to a weaker label while `_run_probe` still ran
    the identity probe, every identity-backed ✓ would be reported as a bare 2xx and
    the whole point of the change would be invisible on the surface it was made for.
    Asserted over the full roster, so a new service inherits the invariant."""
    from ops.spec import build_services

    for s in build_services():
        kind, _target = observe._probe_kind_target(s)
        if s.identity_probe is not None:
            assert kind == "identity", f"{s.session} runs an identity probe but reports {kind!r}"
        else:
            assert kind != "identity", f"{s.session} reports identity without a probe to back it"


def test_observe_carries_the_identity_probes_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failing fact rides out with the verdict. A bare `alive=False` flattens
    "nothing is listening" and "another cluster's gateway holds this port" into one
    answer, and those call for opposite actions — the observation layer was the last
    surface still dropping the difference."""
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(observe, "_curl_ok", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.daemon_health._probe_daemon",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.port_taken("home='/home/ava/.ava' != '/Users/z/.ava'"),  # pyright: ignore[reportUnknownArgumentType]
    )
    statuses = {s.session: s for s in observe.observe_services(frozenset({"gateway"}))}
    assert "/home/ava/.ava" in statuses["gateway"].detail


def test_observe_leaves_detail_empty_on_a_healthy_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A passing probe adds nothing: `detail` is for what went wrong, so a consumer
    can treat "non-empty" as "there is something to say"."""
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(observe, "_curl_ok", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.daemon_health._probe_daemon",
        lambda *_a, **_kw: DaemonProbe.up("ok"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("shared.daemon_health._probe_home", lambda *_a, **_kw: DaemonProbe.up("ok"))  # pyright: ignore[reportUnknownArgumentType]
    statuses = {s.session: s for s in observe.observe_services(frozenset({"gateway"}))}
    assert statuses["gateway"].alive is True
    assert statuses["gateway"].detail == ""
    assert statuses["frontend"].detail == ""  # liveness-only, and it passed


def test_observe_liveness_only_failure_says_which_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The weaker signals report their own words too, so `_run_probe` stays faithful
    to `cli.commands._probe._probe_service` in what it reports, not just in its
    precedence — the two drifting is the bug this module's roster exists to prevent."""
    from shared.config import settings
    from shared.daemon_health import DaemonProbe

    # This test exercises the probe-failure message per signal kind, so milvus
    # must be on the probe set: pin the milvus memory-search backend (numpy,
    # the default, gates the milvus service out with its own gated status).
    monkeypatch.setattr(settings.services, "memory_search_backend", "milvus")
    monkeypatch.setattr(observe, "_curl_ok", lambda _url: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(observe, "_tcp_ok", lambda _port: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.daemon_health._probe_daemon",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.down("x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    statuses = {s.session: s for s in observe.observe_services(frozenset({"gateway"}))}
    assert "no 2xx/3xx from" in statuses["frontend"].detail
    assert "nothing accepting on port" in statuses["milvus"].detail
