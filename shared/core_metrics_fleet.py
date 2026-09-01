"""Core Fleet growth panels (task #2010) — a separate registration module.

The two Fleet growth panels read the gateway's absolute agent-registry
max-id gauge (``gateway/_agent_max_id.py``, 60s sample): a Prometheus
timeseries of ``ava_agent_registry_max_id_ratio`` and its ``deriv()`` slope
in agents per day. They live here — beside ``core_metrics_dismissed`` — so
the core panels module (already at 787 lines) stays untouched, while the
ava_observability registry count remains locked at 21 metrics by tests.
"""

from __future__ import annotations

from shared import core_metrics
from shared.plugin_metrics import MetricSpec

core_metrics.register_core_metric(
    MetricSpec(
        name="core_agent_max_id",
        title="Max Agent ID",
        event_name="agent_registry",
        category="telemetry",
        unit="short",
        panel="timeseries",
        # The absolute registry high-water mark, sampled by the gateway every
        # 60s (gateway/_agent_max_id.py, task #2010). Unit-"1" gauges export
        # with the `_ratio` suffix (same naming as the resolution_status
        # tiles). The gauge is per gateway process and the process restarts on
        # every rollout, so a bare read draws one overlapping series per
        # gateway lifetime; max() collapses them into the single high-water
        # curve the panel promises.
        query="max(ava_agent_registry_max_id_ratio)",
        query_type="promql",
        target_names=["max_id"],
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="core_agent_max_id_growth_rate",
        title="Max Agent ID growth rate",
        event_name="agent_registry",
        category="telemetry",
        unit="short",
        panel="timeseries",
        # The growth curve's slope, extrapolated to agents per day — the
        # deriv() over the last hour on the max()-collapsed 60s gauge (task
        # #2010; the [1h:] subquery form — a bare [1h] range on a function
        # call is a PromQL parse error). Shows batch-spawn intensity at a
        # glance (e.g. +300/day).
        query="deriv(max(ava_agent_registry_max_id_ratio)[1h:]) * 86400",
        query_type="promql",
        target_names=["agents/day"],
    )
)
