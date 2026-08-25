"""OTel Collector sidecar healthcheck — called every 60s by the watchdog.

Probes the sidecar's OTLP/HTTP receiver at 127.0.0.1:4318 with a valid empty
ExportTraceServiceRequest and requires a 2xx. A socket that merely answers 401
or 415 is not a working ingestion path. It also reads the collector's pinned
loopback Prometheus endpoint (the per-unit AVA_OTELCOL_METRICS_PORT, default
8888) for current exporter queue saturation. Remote pressure is logged, not
"fixed" by restarting a healthy local process; the self-scrape in the
collector config exports the same queue/drop metrics to central Prometheus for
alerting.

The sidecar is the local OTLP entry for every agent on this machine — except
that a gateway without its home's ``lgtm-host`` marker (and without an
explicit ``AVA_TELEMETRY_OTLP_ENDPOINT`` override) has no local collector
responsibility and its healthcheck warns/returns; pure runners retain relay
collector behavior. When responsible and down, trace/event/metric export
drops (agents retry briefly, then shed) until the watchdog revives it within a
minute.
"""

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.log import init_gateway_process, logger
from shared.machine import MachineRoleInvalid, MachineRoleMissing, machine_role
from shared.observability import collector_allowed_for_home, gateway_observability_home
from shared.paths import otel_collector_binary, otel_collector_config
from shared.service_respawn import respawn_and_verify, run_keepalive
from shared.supervised_listener import (
    probe_supervised_listener,
    reclaim_stale_supervised_listener,
)

_log = logging.getLogger("services.healthchecks.otel_collector")
_QUEUE_SAMPLE = re.compile(
    r"^otelcol_exporter_queue_(?P<kind>capacity|size)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_ENQUEUE_FAILURE_SAMPLE = re.compile(
    r"^otelcol_exporter_enqueue_failed_[^{]+\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_LABEL = re.compile(r'(?:^|,)\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>[^"]*)"')
_COLLECTOR_RESPAWN_TIMEOUT_S = 5.0


def _collector_serves_this_home() -> bool:
    """Whether this unit owns a collector that the healthcheck should probe."""
    try:
        roles = machine_role()
    except (MachineRoleMissing, MachineRoleInvalid):
        return False
    if "gateway" not in roles:
        return True
    return collector_allowed_for_home(gateway_observability_home())


@dataclass(frozen=True)
class CollectorPressure:
    saturated: tuple[str, ...]
    enqueue_failures: dict[str, int]


def _metrics_url() -> str:
    """This unit's collector self-metrics endpoint, shared with converge config."""
    return f"http://localhost:{settings.observability.otel_collector_metrics_port}/metrics"


def _collector_ports() -> tuple[int, int]:
    """Listener ports owned by this unit's collector process."""
    return (4318, settings.observability.otel_collector_metrics_port)


def _labels(text: str) -> dict[str, str]:
    return {match.group("key"): match.group("value") for match in _LABEL.finditer(text)}


def _is_alive() -> bool:
    """A valid OTLP/JSON request accepted by the local pipeline = alive."""
    try:
        req = urllib.request.Request(  # noqa: S310 — loopback probe, deliberate
            urllib.parse.urljoin("http://127.0.0.1:4318/", "v1/traces"),
            method="POST",
            data=b'{"resourceSpans":[]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0):  # noqa: S310 — same probe
            return True
    except Exception:
        return False


def probe_collector() -> DaemonProbe:
    """The collector is alive only when its OTLP response and supervisor agree."""
    listener = probe_supervised_listener(
        "otel-collector", ports=_collector_ports(), binary=otel_collector_binary()
    ).probe
    if not listener.alive:
        return listener
    if not _is_alive():
        return DaemonProbe.down("supervised collector does not accept a valid OTLP trace request")
    return listener


def take_over_stale_collector() -> None:
    """Evict only same-binary collector listeners without a live session record."""
    reclaim_stale_supervised_listener(
        "otel-collector",
        ports=_collector_ports(),
        binary=otel_collector_binary(),
        grace_s=_COLLECTOR_RESPAWN_TIMEOUT_S,
    )


def _queue_pressure() -> CollectorPressure | None:
    """Current saturation plus lifetime enqueue-failure counters.

    The counter is reported only as context while a queue is currently full;
    it is monotone for the collector process and therefore must never, by
    itself, create a permanent warning after recovery. Central alerting uses
    `increase(...[5m])` on the self-scraped series instead.
    """
    try:
        with urllib.request.urlopen(_metrics_url(), timeout=2.0) as response:  # noqa: S310 — fixed loopback probe
            payload = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    capacities: dict[str, float] = {}
    sizes: dict[str, float] = {}
    failures: dict[str, int] = {}
    for line in payload.splitlines():
        queue_match = _QUEUE_SAMPLE.match(line)
        if queue_match:
            exporter = _labels(queue_match.group("labels")).get("exporter")
            if exporter:
                target = capacities if queue_match.group("kind") == "capacity" else sizes
                target[exporter] = float(queue_match.group("value"))
            continue
        failure_match = _ENQUEUE_FAILURE_SAMPLE.match(line)
        if failure_match:
            exporter = _labels(failure_match.group("labels")).get("exporter")
            if exporter:
                failures[exporter] = failures.get(exporter, 0) + int(
                    float(failure_match.group("value"))
                )
    saturated = tuple(
        sorted(
            exporter
            for exporter, capacity in capacities.items()
            if capacity > 0 and sizes.get(exporter, 0) >= capacity
        )
    )
    return CollectorPressure(saturated=saturated, enqueue_failures=failures)


def _restart_daemon() -> DaemonProbe:
    """Take over a verified stale collector, then respawn and verify its replacement."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    take_over_stale_collector()
    return respawn_and_verify(
        "otel-collector",
        f"{otel_collector_binary()} --config {otel_collector_config()}",
        project_root,
        verify=probe_collector,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        graceful_timeout_s=_COLLECTOR_RESPAWN_TIMEOUT_S,
    )


def main() -> None:
    init_gateway_process("otel-collector")
    if not _collector_serves_this_home():
        logger.bind(_no_emitter=True, component="otel-collector-healthcheck").warning(
            "collector skipped — this gateway home is not the LGTM host; telemetry export is unavailable"
        )
        return
    if not probe_collector().alive:
        run_keepalive("otel-collector", _log, probe=probe_collector, respawn=_restart_daemon)
        return
    pressure = _queue_pressure()
    if pressure is None:
        logger.bind(_no_emitter=True, component="otel-collector-healthcheck").warning(
            "collector ingestion is alive but internal queue metrics are unreadable at {}",
            _metrics_url(),
        )
    elif pressure.saturated:
        failed = sum(pressure.enqueue_failures.get(name, 0) for name in pressure.saturated)
        logger.bind(_no_emitter=True, component="otel-collector-healthcheck").warning(
            "collector exporter queue saturated: exporters={} lifetime enqueue failures={}",
            ",".join(pressure.saturated),
            failed,
        )


if __name__ == "__main__":
    main()
