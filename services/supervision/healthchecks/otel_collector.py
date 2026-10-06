"""Read-only health probes for otel collector; the root supervisor owns recovery."""

import re
import urllib.request
from dataclasses import dataclass
from http.client import HTTPException

from base.cluster.machine import MachineRoleInvalid, MachineRoleMissing, machine_role
from base.config import settings
from base.daemon.health import DaemonProbe
from base.telemetry.observability import collector_allowed_for_home, gateway_observability_home

_QUEUE_SAMPLE = re.compile(
    r"^otelcol_exporter_queue_(?P<kind>capacity|size)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_ENQUEUE_FAILURE_SAMPLE = re.compile(
    r"^otelcol_exporter_enqueue_failed_[^{]+\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_LABEL = re.compile(r'(?:^|,)\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>[^"]*)"')


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


def _labels(text: str) -> dict[str, str]:
    return {match.group("key"): match.group("value") for match in _LABEL.finditer(text)}


def _is_alive() -> bool:
    """A valid OTLP/JSON request accepted by the local pipeline = alive."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.observability.telemetry_otlp_port}/v1/traces",
            method="POST",
            data=b'{"resourceSpans":[]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0):  # noqa: S310 — same probe
            return True
    except (OSError, HTTPException):
        return False


def probe_collector() -> DaemonProbe:
    """Observe whether the collector accepts a valid OTLP trace request."""
    if _is_alive():
        return DaemonProbe.up("collector accepts OTLP trace requests")
    return DaemonProbe.down("collector does not accept a valid OTLP trace request")


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
    except (OSError, HTTPException):
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
