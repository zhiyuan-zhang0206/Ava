"""Read-only probes for the designated observability station.

The endpoint probes observe local Loki, Prometheus and Grafana readiness. The
write-path probe sends a unique Loki log and queries it back, with bounded
admission retries. Probe results carry no authority to restart a backend;
service lifecycle belongs to the root supervisor.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from shared.daemon_health import DaemonProbe
from shared.paths import ava_home

_local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NANOSECONDS_PER_SECOND = 1_000_000_000
_WRITE_PROBE_LOOKBACK_SECONDS = 120
_WRITE_PROBE_END_LAG_SECONDS = 1
# Two bounded retries for Loki admission throttling; one probe makes at most three pushes.
_WRITE_PROBE_RETRY_BACKOFF_SECONDS = (1, 2)


def readiness_probes() -> tuple[tuple[str, str], ...]:
    """Probe this home's native bind settings, independent of remote query URLs.

    Tempo is remote and cannot trigger a local backend restart.
    """
    from shared.lgtm_local import BACKENDS, HEALTH_PATHS, backend_urls

    urls = backend_urls()
    return tuple((name, f"{urls[name]}{HEALTH_PATHS[name]}") for name in BACKENDS)


def lgtm_host_marker() -> Path:
    """The machine-identity marker that designates THIS host as the one running
    the local LGTM backends (`$AVA_HOME/lgtm-host`, operator-created once)."""
    return ava_home() / "lgtm-host"


def is_lgtm_host() -> bool:
    """Whether this host is the observability station: the `lgtm-host` marker
    OR the declarative `observability-station` unit capability."""
    from shared.observability import home_is_observability_station

    return home_is_observability_station(ava_home())


def _protocol_readiness(name: str) -> DaemonProbe:
    from shared.lgtm_local import HEALTH_PATHS, backend_urls

    url = backend_urls()[name] + HEALTH_PATHS[name]
    try:
        with _local_http.open(url, timeout=2.0) as response:
            if not 200 <= response.status < 300:
                return DaemonProbe.down(f"{name} readiness HTTP {response.status}")
            if name == "grafana" and json.loads(response.read())["database"] != "ok":
                return DaemonProbe.down("Grafana database is not ready")
    except (OSError, ValueError, KeyError) as exc:
        return DaemonProbe.down(f"{name} readiness failed: {type(exc).__name__}")
    return DaemonProbe.up(f"{name} readiness accepted")


def probe_backend(name: str) -> DaemonProbe:
    """Require native root ownership and a successful backend readiness response."""
    from functools import partial

    from services.healthchecks.owned_service import probe_endpoint
    from shared.lgtm_local import backend_urls

    port = urllib.parse.urlsplit(backend_urls()[name]).port
    if port is None:
        return DaemonProbe.unavailable("backend has no explicit local port")
    return probe_endpoint(name, port, partial(_protocol_readiness, name))


def probe_statuses() -> list[tuple[str, bool]]:
    from shared.lgtm_local import BACKENDS

    return [(name, probe_backend(name).alive) for name in BACKENDS]


def write_path_probe() -> tuple[bool, str]:
    """Send one unique OTLP log and verify that Loki made it queryable."""
    now_ns = time.time_ns()
    marker_ns = now_ns - (_WRITE_PROBE_END_LAG_SECONDS * _NANOSECONDS_PER_SECOND)
    marker = f"watchdog-write-probe-{marker_ns}"
    from shared.lgtm_local import backend_urls

    base_url = backend_urls()["loki"]
    body = json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "agent_id", "value": {"stringValue": "watchdog"}},
                            {
                                "key": "event_name",
                                "value": {"stringValue": "watchdog-write-probe"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": str(marker_ns),
                                    "body": {"stringValue": marker},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    ).encode()
    push_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/otlp/v1/logs",
        data=body,
        headers={"Content-Type": "application/json", "X-Scope-OrgID": "fake"},
        method="POST",
    )
    push_failure = _push_probe(push_request)
    if push_failure is not None:
        return False, push_failure

    # The range end is exclusive, so it must sit strictly past the pushed
    # line's timestamp. Capture a fresh now after the push (never the
    # pre-push marker time) and anchor the window on the marker instead.
    query_end_ns = time.time_ns()
    query = urllib.parse.urlencode(
        {
            "query": f'{{agent_id="watchdog", event_name="watchdog-write-probe"}} |= "{marker}"',
            "start": str(marker_ns - (_WRITE_PROBE_LOOKBACK_SECONDS * _NANOSECONDS_PER_SECOND)),
            "end": str(query_end_ns),
        }
    )
    query_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/loki/api/v1/query_range?{query}",
        headers={"X-Scope-OrgID": "fake"},
    )
    try:
        with _local_http.open(query_request, timeout=2.0) as response:
            if not 200 <= response.status < 300:
                return False, "query_error"
            payload: dict[str, Any] = json.loads(response.read())
            visible = any(
                value[1] == marker
                for stream in payload["data"]["result"]
                for value in stream["values"]
            )
    except Exception:
        return False, "query_error"
    return (True, "ok") if visible else (False, "probe_not_visible")


def _push_probe(request: urllib.request.Request) -> str | None:
    """Push once, retrying only HTTP 429 with bounded backoff."""
    for attempt in range(len(_WRITE_PROBE_RETRY_BACKOFF_SECONDS) + 1):
        try:
            with _local_http.open(request, timeout=2.0) as response:
                status = response.status
                response_body = response.read() if status >= 500 else b""
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read() if status >= 500 else b""
        except Exception:
            return "push_error"
        if 200 <= status < 300:
            return None
        if status == 429 and attempt < len(_WRITE_PROBE_RETRY_BACKOFF_SECONDS):
            time.sleep(_WRITE_PROBE_RETRY_BACKOFF_SECONDS[attempt])
            continue
        return _push_failure_reason(status, response_body)
    raise AssertionError("unreachable write-probe retry state")


def _push_failure_reason(status: int, response_body: bytes) -> str:
    if status >= 500 and b"ingester is shutting down" in response_body.lower():
        return "ingester_shutting_down"
    return f"push_http_{status}"
