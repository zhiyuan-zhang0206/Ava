"""Collector health uses the OTLP protocol response."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from base.cluster import port_preflight
from base.cluster.machine import (
    MachineIdentity,
    MachineRoleInvalid,
    MachineRoleMissing,
    MachineRoles,
)
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.native_process.root_control import client
from services.supervision.healthchecks import otel_collector as hc


def _role_machine(read_role: Callable[[], MachineRoles]) -> MachineIdentity:
    return MachineIdentity(
        name=lambda: "collector",
        role=read_role,
        host=lambda: "localhost",
        description=lambda: None,
    )


@pytest.mark.parametrize("ready", [True, False])
def test_collector_health_uses_otlp_without_native_inspection(
    monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("collector protocol health must not inspect listeners")

    monkeypatch.setattr(port_preflight, "strict_listeners_on", forbidden)
    monkeypatch.setattr(client, "root_process", forbidden)
    monkeypatch.setattr(hc, "is_alive", lambda: ready)
    assert hc.probe_collector().alive is ready


def test_collector_serves_this_home_fails_closed_without_machine_role(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    def missing() -> MachineRoles:
        raise MachineRoleMissing

    def invalid() -> MachineRoles:
        raise MachineRoleInvalid("bad")

    assert hc.collector_serves_this_home(machine=_role_machine(missing)) is False
    with pytest.raises(MachineRoleInvalid, match="bad"):
        hc.collector_serves_this_home(machine=_role_machine(invalid))


def test_collector_serves_this_home_keeps_runner_and_unconfigured_behavior(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role=frozenset({"agent-runner"}))
    assert hc.collector_serves_this_home(machine=machine_identity) is True

    machine_identity.set(role=frozenset({"gateway"}))

    def unconfigured(machine: MachineIdentity, *, home: Path) -> None:
        assert machine is machine_identity and home.is_dir()

    monkeypatch.setattr(hc, "gateway_observability_home", unconfigured)
    assert hc.collector_serves_this_home(machine=machine_identity) is True


def test_collector_serves_this_home_requires_gateway_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role=frozenset({"gateway", "agent-runner"}))

    def gateway_home(machine: MachineIdentity, *, home: Path) -> Path:
        assert machine is machine_identity and home.is_dir()
        return tmp_path

    monkeypatch.setattr(hc, "gateway_observability_home", gateway_home)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert hc.collector_serves_this_home(machine=machine_identity) is False
    (tmp_path / "lgtm-host").touch()
    assert hc.collector_serves_this_home(machine=machine_identity) is True


def test_collector_serves_this_home_with_explicit_endpoint_skips_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, machine_identity: MachineIdentity
) -> None:
    """An explicit MEIVA_TELEMETRY_OTLP_ENDPOINT opens the healthcheck's
    collector responsibility on a non-LGTM gateway (marker OR override)."""
    machine_identity.set(role=frozenset({"gateway", "agent-runner"}))

    def gateway_home(machine: MachineIdentity, *, home: Path) -> Path:
        assert machine is machine_identity and home.is_dir()
        return tmp_path

    monkeypatch.setattr(hc, "gateway_observability_home", gateway_home)
    monkeypatch.setitem(
        os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318"
    )
    assert hc.collector_serves_this_home(machine=machine_identity) is True


def test_is_alive_rejecting_valid_otlp_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listener that rejects the collector's valid OTLP body is not a healthy
    ingestion pipeline; a bare socket answer is insufficient."""

    def _raise(_req, **_kw):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:4318/v1/traces",
            415,
            "Unsupported Media Type",
            {},  # pyright: ignore[reportArgumentType]
            None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc.is_alive() is False


def test_is_alive_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx proves the receiver parsed and accepted a valid OTLP request."""

    seen: dict[str, object] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(req: urllib.request.Request, **_kw: object) -> _Resp:
        seen["url"] = req.full_url
        seen["body"] = req.data
        seen["content_type"] = req.headers["Content-type"]
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert hc.is_alive() is True
    assert seen == {
        "url": "http://127.0.0.1:4318/v1/traces",
        "body": b'{"resourceSpans":[]}',
        "content_type": "application/json",
    }


def test_is_alive_uses_local_port_despite_export_endpoint_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote or stale producer export overrides cannot redirect local liveness."""
    monkeypatch.setattr(hc.settings.observability, "telemetry_otlp_port", 4319)
    seen: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(req: urllib.request.Request, **_kw: object) -> _Resp:
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(
        hc.settings.observability,
        "telemetry_otlp_endpoint",
        "http://collector.example:4318/base",
    )
    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert hc.is_alive() is True
    assert seen == ["http://127.0.0.1:4319/v1/traces"]


def test_is_alive_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-level failure — the sidecar is down."""

    def _raise(_req, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc.is_alive() is False


def test_queue_pressure_uses_configured_self_metrics_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog probes the same per-unit endpoint that converge renders."""
    seen: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def _open(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(hc.settings.observability, "otel_collector_metrics_port", 8889)
    monkeypatch.setattr(urllib.request, "urlopen", _open)

    assert hc.queue_pressure() == hc.CollectorPressure(saturated=(), enqueue_failures={})
    assert seen == ["http://localhost:8889/metrics"]


def test_queue_pressure_reports_full_queue_and_drop_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collector internal metrics make the exact queue and rejected-item count
    visible; the healthcheck does not infer pressure from process liveness."""
    payload = b"""\
otelcol_exporter_queue_capacity{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_queue_size{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_enqueue_failed_metric_points{exporter="otlphttp/prometheus"} 78336
otelcol_exporter_queue_capacity{data_type="logs",exporter="otlphttp/loki"} 5000
otelcol_exporter_queue_size{data_type="logs",exporter="otlphttp/loki"} 12
"""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: _Resp())  # pyright: ignore[reportUnknownArgumentType]
    pressure = hc.queue_pressure()
    assert pressure is not None
    assert pressure.saturated == ("otlphttp/prometheus",)
    assert pressure.enqueue_failures == {"otlphttp/prometheus": 78336}
