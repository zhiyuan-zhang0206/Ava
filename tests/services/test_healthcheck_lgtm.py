"""Native readiness and Loki write/read protocol tests."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from services.healthchecks import lgtm as hc
from shared.config import settings
from shared.daemon_health import DaemonProbe


class _Response:
    def __init__(self, *, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_readiness_probes_exclude_remote_tempo() -> None:
    """Only locally managed backends can trigger a local lifecycle repair —
    and the default settings keep the historical loopback probe URLs exactly."""
    assert hc.readiness_probes() == (
        ("loki", "http://127.0.0.1:3100/ready"),
        ("prometheus", "http://127.0.0.1:9090/-/ready"),
        ("grafana", "http://127.0.0.1:3003/api/health"),
    )


def test_readiness_probes_ignore_remote_query_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote read endpoints cannot falsely mark a local native service alive."""
    monkeypatch.setattr(
        settings.observability, "telemetry_loki_url", "http://loki.example:3100/loki/"
    )
    monkeypatch.setattr(
        settings.observability, "telemetry_prometheus_url", "http://prom.example:9090/"
    )
    monkeypatch.setattr(
        settings.observability, "telemetry_grafana_url", "http://grafana.example:3003"
    )
    assert hc.readiness_probes() == (
        ("loki", "http://127.0.0.1:3100/ready"),
        ("prometheus", "http://127.0.0.1:9090/-/ready"),
        ("grafana", "http://127.0.0.1:3003/api/health"),
    )


def test_write_path_probe_rejects_400_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError("http://loki/otlp/v1/logs", 400, "rejected", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_http_400")


def test_write_path_probe_retries_429_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    sleeps: list[int] = []

    def _reject(request: urllib.request.Request, **_kwargs: object) -> None:
        requests.append(request)
        raise urllib.error.HTTPError(  # pyright: ignore[reportArgumentType]
            request.full_url, 429, "throttled", email.message.Message(), None
        )

    monkeypatch.setattr(hc._local_http, "open", _reject)
    monkeypatch.setattr(hc.time, "sleep", sleeps.append)

    assert hc.write_path_probe() == (False, "push_http_429")
    assert len(requests) == 3
    assert requests[0] is requests[1] is requests[2]
    assert sleeps == [1, 2]


def test_write_path_probe_identifies_stuck_ingester(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(  # pyright: ignore[reportArgumentType]
            "http://loki/otlp/v1/logs",
            500,
            "internal error",
            email.message.Message(),
            io.BytesIO(b"RPC error: code = Unknown desc = InGeStEr Is ShUtTiNg DoWn"),
        )

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "ingester_shutting_down")


def test_write_path_probe_does_not_misclassify_plain_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(  # pyright: ignore[reportArgumentType]
            "http://loki/otlp/v1/logs",
            503,
            "throttled",
            email.message.Message(),
            io.BytesIO(b"write throttled because disk usage is too high"),
        )

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_http_503")


def test_write_path_probe_reports_push_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_error")


def test_write_path_probe_reports_marker_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, **_kwargs: object) -> _Response:
        requests.append(request)
        if len(requests) == 1:
            return _Response(status=204)
        return _Response(status=200, body=b'{"data":{"result":[]}}')

    monkeypatch.setattr(hc._local_http, "open", _open)

    assert hc.write_path_probe() == (False, "probe_not_visible")
    request_body = requests[0].data
    assert isinstance(request_body, bytes)
    payload = cast(dict[str, Any], json.loads(request_body))
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    timestamp = record["timeUnixNano"]
    marker = record["body"]["stringValue"]
    assert timestamp.isdigit()
    assert marker == f"watchdog-write-probe-{timestamp}"
    assert requests[0].full_url.endswith("/otlp/v1/logs")
    assert requests[0].get_header("X-scope-orgid") == "fake"
    assert requests[0].get_header("Content-type") == "application/json"


def test_write_path_probe_finds_marker_in_numeric_query_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    marker = ""

    def _open(request: urllib.request.Request, **_kwargs: object) -> _Response:
        nonlocal marker
        requests.append(request)
        if len(requests) == 1:
            request_body = request.data
            assert isinstance(request_body, bytes)
            payload = cast(dict[str, Any], json.loads(request_body))
            resource_log = payload["resourceLogs"][0]
            attributes = {
                attribute["key"]: attribute["value"]["stringValue"]
                for attribute in resource_log["resource"]["attributes"]
            }
            record = resource_log["scopeLogs"][0]["logRecords"][0]
            marker = record["body"]["stringValue"]
            assert attributes == {
                "agent_id": "watchdog",
                "event_name": "watchdog-write-probe",
            }
            assert record["timeUnixNano"] == marker.removeprefix("watchdog-write-probe-")
            return _Response(status=204)
        body = json.dumps({"data": {"result": [{"values": [["1", marker]]}]}}).encode()
        return _Response(status=200, body=body)

    monkeypatch.setattr(hc._local_http, "open", _open)

    assert hc.write_path_probe() == (True, "ok")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[1].full_url).query)
    assert query["query"] == [
        f'{{agent_id="watchdog", event_name="watchdog-write-probe"}} |= "{marker}"'
    ]
    assert query["start"][0].isdigit()
    assert query["end"][0].isdigit()
    marker_ts = int(marker.removeprefix("watchdog-write-probe-"))
    assert int(query["end"][0]) > marker_ts  # range end is exclusive
    assert int(query["end"][0]) - int(query["start"][0]) >= 121_000_000_000
    assert "/loki/api/v1/query_range?" in requests[1].full_url


def test_write_path_probe_reports_query_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def _open(_request: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(status=204)
        raise OSError("query unavailable")

    monkeypatch.setattr(hc._local_http, "open", _open)

    assert hc.write_path_probe() == (False, "query_error")


def test_is_lgtm_host_accepts_station_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The declarative observability-station capability designates the host
    exactly like the marker — the watchdog keepalive and `ava status` gate on
    either form."""
    from shared.machine import reset_identity, set_identity

    home = tmp_path / "station"
    home.mkdir()
    # is_lgtm_host() dials the home twice: the name bound in this module
    # (from shared.paths import ava_home) AND the fresh lookup inside
    # home_is_observability_station — patch both.
    monkeypatch.setattr(hc, "ava_home", lambda: home)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)

    # No marker, no capability -> not the station.
    assert hc.is_lgtm_host() is False

    # Capability form.
    set_identity(role="observability-station")
    try:
        assert hc.is_lgtm_host() is True
    finally:
        reset_identity()
    assert hc.is_lgtm_host() is False

    # Marker form.
    (home / "lgtm-host").touch()
    assert hc.is_lgtm_host() is True


@pytest.mark.parametrize("status", [301, 401, 500, 503])
def test_unready_http_cannot_certify_backend(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def response(*_args: object, **_kw: object) -> _Response:
        return _Response(status=status)

    monkeypatch.setattr(hc._local_http, "open", response)
    assert not hc._protocol_readiness("loki").alive


@pytest.mark.parametrize("database,ready", [("ok", True), ("failed", False)])
def test_grafana_health_requires_database_ready(
    monkeypatch: pytest.MonkeyPatch, database: str, ready: bool
) -> None:
    def response(*_args: object, **_kw: object) -> _Response:
        return _Response(status=200, body=json.dumps({"database": database}).encode())

    monkeypatch.setattr(hc._local_http, "open", response)
    assert hc._protocol_readiness("grafana").alive is ready


def test_unowned_listener_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_owned(name: str, _port: int, _protocol: Callable[[], DaemonProbe]) -> DaemonProbe:
        assert name == "loki"
        return DaemonProbe.port_taken("foreign process")

    monkeypatch.setattr("services.healthchecks.owned_service.probe_endpoint", not_owned)
    assert not hc.probe_backend("loki").alive
