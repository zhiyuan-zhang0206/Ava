"""Collector protocol health must belong to root-owned listeners."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from services.healthchecks import otel_collector as hc
from shared.machine import MachineRoleInvalid, MachineRoleMissing


@pytest.mark.parametrize("owned", [True, False])
def test_collector_protocol_success_requires_root_owned_listeners(
    monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    from shared.native_process.ownership import OwnedProcess
    from shared.root_control import client

    owner = OwnedProcess(101, 12.0, None)

    def _fake_owned_process(_unit: str) -> OwnedProcess | None:
        return owner

    def _fake_strict_listeners_on(_port: int) -> list[int]:
        return [202]

    def _fake_leader_owns_pids(_expected: OwnedProcess, _pids: set[int]) -> bool:
        return owned

    monkeypatch.setattr(client, "owned_process", _fake_owned_process)
    monkeypatch.setattr(hc, "strict_listeners_on", _fake_strict_listeners_on)
    monkeypatch.setattr(hc, "leader_owns_pids", _fake_leader_owns_pids)
    monkeypatch.setattr(hc, "_is_alive", lambda: True)

    result = hc.probe_collector()
    assert result.alive is owned
    assert result.terminal is not owned


def test_collector_cannot_certify_a_listener_without_root_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.daemon_health import ProbeVerdict
    from shared.native_process.ownership import OwnedProcess
    from shared.root_control import client

    def _fake_owned_process(_unit: str) -> OwnedProcess | None:
        return None

    def _fake_strict_listeners_on(_port: int) -> list[int]:
        return [202]

    monkeypatch.setattr(client, "owned_process", _fake_owned_process)
    monkeypatch.setattr(hc, "strict_listeners_on", _fake_strict_listeners_on)
    monkeypatch.setattr(hc, "_is_alive", lambda: pytest.fail("unknown ownership must not pass"))
    assert hc.probe_collector().verdict is ProbeVerdict.UNAVAILABLE


def test_collector_discovery_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.daemon_health import ProbeVerdict
    from shared.port_preflight import ListenerDiscoveryError

    def fail(_port: int) -> list[int]:
        raise ListenerDiscoveryError("cannot inspect listeners")

    monkeypatch.setattr(hc, "strict_listeners_on", fail)
    assert hc.probe_collector().verdict is ProbeVerdict.UNAVAILABLE


def test_collector_root_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.daemon_health import ProbeVerdict
    from shared.root_control import client

    def fail(_unit: str) -> None:
        raise client.RootClientError("root status unavailable")

    def _fake_strict_listeners_on(_port: int) -> list[int]:
        return [202]

    monkeypatch.setattr(client, "owned_process", fail)
    monkeypatch.setattr(hc, "strict_listeners_on", _fake_strict_listeners_on)
    assert hc.probe_collector().verdict is ProbeVerdict.UNAVAILABLE


def test_collector_serves_this_home_fails_closed_without_machine_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hc, "machine_role", lambda: (_ for _ in ()).throw(MachineRoleMissing()))
    assert hc._collector_serves_this_home() is False

    monkeypatch.setattr(
        hc, "machine_role", lambda: (_ for _ in ()).throw(MachineRoleInvalid("bad"))
    )
    assert hc._collector_serves_this_home() is False


def test_collector_serves_this_home_keeps_runner_and_unconfigured_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"agent-runner"}))
    assert hc._collector_serves_this_home() is True

    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(hc, "gateway_observability_home", lambda: None)
    assert hc._collector_serves_this_home() is True


def test_collector_serves_this_home_requires_gateway_lgtm_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(hc, "gateway_observability_home", lambda: tmp_path)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert hc._collector_serves_this_home() is False
    (tmp_path / "lgtm-host").touch()
    assert hc._collector_serves_this_home() is True


def test_collector_serves_this_home_with_explicit_endpoint_skips_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit AVA_TELEMETRY_OTLP_ENDPOINT opens the healthcheck's
    collector responsibility on a non-LGTM gateway (marker OR override)."""
    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(hc, "gateway_observability_home", lambda: tmp_path)
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    assert hc._collector_serves_this_home() is True


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
    assert hc._is_alive() is False


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
    assert hc._is_alive() is True
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
    assert hc._is_alive() is True
    assert seen == ["http://127.0.0.1:4319/v1/traces"]


def test_is_alive_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-level failure — the sidecar is down."""

    def _raise(_req, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


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

    assert hc._queue_pressure() == hc.CollectorPressure(saturated=(), enqueue_failures={})
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
    pressure = hc._queue_pressure()
    assert pressure is not None
    assert pressure.saturated == ("otlphttp/prometheus",)
    assert pressure.enqueue_failures == {"otlphttp/prometheus": 78336}
