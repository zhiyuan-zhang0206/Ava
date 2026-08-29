"""Core Grafana tiles for class-resolution dismissed counts (task #1935).

The events-maintenance daemon publishes dismissed counts beside the
unresolved ones; these two tiles complete the total / resolved / net trio
with the Loki Warning / Error tiles (core_metrics_panels.py) and the
unresolved tiles. PromQL static expressions, like their unresolved
counterparts. The gauge names carry the `_ratio` suffix the OTel
Prometheus exporter appends to unit-"1" instruments. Registered as a
separate module so the core observability pack registry (locked at 19
metrics by tests) stays untouched.
"""

from __future__ import annotations

from shared import core_metrics
from shared.plugin_metrics import MetricSpec

core_metrics.register_core_metric(
    MetricSpec(
        name="core_dismissed_warning",
        title="Dismissed Warning",
        event_name="resolution_status",
        category="telemetry",
        unit="short",
        panel="stat",
        query="ava_resolution_status_dismissed_warnings_ratio",
        query_type="promql",
        target_names=["dismissed_warning"],
        field_defaults={"color": {"mode": "fixed", "fixedColor": "orange"}},
        width=8,
        height=4,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_dismissed_error",
        title="Dismissed Error",
        event_name="resolution_status",
        category="telemetry",
        unit="short",
        panel="stat",
        query="ava_resolution_status_dismissed_errors_ratio",
        query_type="promql",
        target_names=["dismissed_error"],
        options={"noValue": "0"},
        field_defaults={"color": {"mode": "fixed", "fixedColor": "red"}},
        width=8,
        height=4,
    )
)
