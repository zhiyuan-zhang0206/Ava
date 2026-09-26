"""Read-only roster classification and faithful service-probe results."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ops import observe
from ops.service_spec import ServiceSpec
from shared.daemon_health import DaemonProbe


def test_probe_set_gateway_classifies_signal_types() -> None:
    views = {v.session: v for v in observe.probe_set(frozenset({"gateway"}))}
    for name in ("gateway", "frontend", "milvus"):
        assert views[name].kind == "identity"
    assert "gateway-watchdog" not in views
    assert views["gateway"].healthcheck_module == "services.healthchecks.gateway"


def test_probe_set_agent_runner_membership() -> None:
    views = {v.session: v for v in observe.probe_set(frozenset({"agent-runner"}))}
    assert set(views) == {
        "ops",
        "page-server",
        "agent-host",
        "browser",
        "browser-mcp",
        "mcp-daemon",
        "computer-mcp",
        "otel-collector",
        "loki",
        "prometheus",
        "grafana",
    }
    assert views["browser-mcp"].kind == "identity"
    assert views["agent-host"].gate_reason is None
    assert views["agent-host"].kind == "identity"
    assert views["agent-host"].healthcheck_module == "services.healthchecks.agent_host"


def _spec(
    *,
    identity_probe: Callable[[], DaemonProbe] | None = None,
    curl_url: str | None = None,
    tcp_port: int | None = None,
) -> ServiceSpec:
    return ServiceSpec(
        session="test",
        cmd="inert",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        identity_probe=identity_probe,
        curl_url=curl_url,
        tcp_port=tcp_port,
    )


def _one_service(
    monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec, gate: str | None = None
) -> None:
    def services(_roles: object) -> tuple[tuple[ServiceSpec, str | None], ...]:
        return ((spec, gate),)

    monkeypatch.setattr(observe, "services_for_capabilities_annotated", services)


def _reachable(_target: object) -> bool:
    return True


def _unreachable(_target: object) -> bool:
    return False


def test_observe_gated_service_reports_na_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected() -> DaemonProbe:
        raise AssertionError("a gated service must not be probed")

    _one_service(monkeypatch, _spec(identity_probe=unexpected), "disabled by configuration")
    (status,) = observe.observe_services(frozenset({"gateway"}))
    assert status.alive is None
    assert status.kind == "gated"
    assert status.gate_reason == "disabled by configuration"


def test_observe_runs_probe_for_active_http_service(monkeypatch: pytest.MonkeyPatch) -> None:
    _one_service(monkeypatch, _spec(curl_url="http://inert.invalid"))
    monkeypatch.setattr(observe, "_curl_ok", _reachable)
    (status,) = observe.observe_services(frozenset({"gateway"}))
    assert status.alive is True
    assert status.kind == "http"


@pytest.mark.parametrize(
    "probe",
    [
        DaemonProbe.port_taken("another unit owns the listener"),
        DaemonProbe.down("root child absent"),
    ],
)
def test_observe_preserves_failed_identity_and_detail(
    monkeypatch: pytest.MonkeyPatch, probe: DaemonProbe
) -> None:
    _one_service(monkeypatch, _spec(identity_probe=lambda: probe, curl_url="http://inert.invalid"))
    monkeypatch.setattr(observe, "_curl_ok", _reachable)
    (status,) = observe.observe_services(frozenset({"gateway"}))
    assert status.kind == "identity"
    assert status.alive is False
    assert status.detail == probe.detail


def test_observe_leaves_detail_empty_on_a_healthy_service(monkeypatch: pytest.MonkeyPatch) -> None:
    _one_service(
        monkeypatch, _spec(identity_probe=lambda: DaemonProbe.up("captured process owns listener"))
    )
    (status,) = observe.observe_services(frozenset({"gateway"}))
    assert status.alive is True
    assert status.detail == ""


def test_reported_kind_agrees_with_the_probe_actually_run() -> None:
    from ops.spec import build_services

    for spec in build_services():
        kind, _target = observe._probe_kind_target(spec)
        assert (kind == "identity") == (spec.identity_probe is not None)


def test_liveness_only_failures_name_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observe, "_curl_ok", _unreachable)
    monkeypatch.setattr(observe, "_tcp_ok", _unreachable)
    for spec, expected in (
        (_spec(curl_url="http://inert.invalid"), "no 2xx/3xx from http://inert.invalid"),
        (_spec(tcp_port=12345), "nothing accepting on port 12345"),
    ):
        _one_service(monkeypatch, spec)
        (status,) = observe.observe_services(frozenset({"gateway"}))
        assert status.alive is False
        assert status.detail == expected
