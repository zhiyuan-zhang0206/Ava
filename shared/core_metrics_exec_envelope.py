"""Exec-envelope transfer-cost panels — task #2174.

Split out of ``shared/core_metrics_observability.py`` when that module hit
the 800-line code-structure ceiling (task #2174): the two panels unwrap the
``exec_envelope`` event (agent/graph/_exec_protocol.py::_log_envelope_transfer)
and group by envelope/op — the transfer-cost display split out of the Exec
outcomes other bucket (PM ruling 2026-08-31).
"""

from __future__ import annotations

from shared import core_metrics
from shared.plugin_metrics import MetricSpec

# The transfer-cost display split from the Exec outcomes other bucket (PM
# ruling 2026-08-31): per-execution request/result envelope sizes and
# serialization cost, unwrapped from the exec_envelope event and grouped by
# envelope/op — reads parse and writes dump on different paths, so a one-sided
# regression must show on its own series.

core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_exec_envelope_size",
        title="Exec envelope size (bytes)",
        description=(
            "Exec envelope transfer size — serialized request/result envelope "
            "bytes moved between the agent and its exec child, unwrapped from "
            "the exec_envelope event's size_bytes attribute "
            "(agent/graph/_exec_protocol.py::_log_envelope_transfer). p50/p95 "
            "percentiles plus max, grouped by envelope/op; a rising request "
            "band is state/payload inflation. The event is excluded from the "
            "Exec outcomes other bucket (PM ruling 2026-08-31) and displays "
            "here instead. event_name='exec_envelope', category='telemetry'."
        ),
        event_name="exec_envelope",
        category="telemetry",
        unit="bytes",
        panel="timeseries",
        query_type="logql",
        query=(
            'quantile_over_time(0.5, {service_name="unknown_service", '
            "event_name={event_name}} | json | "
            "category={category} | unwrap attributes_size_bytes "
            "[$__interval]) by (attributes_envelope, attributes_op)"
        ),
        targets=[
            (
                'quantile_over_time(0.95, {service_name="unknown_service", '
                "event_name={event_name}} | json | "
                "category={category} | unwrap attributes_size_bytes "
                "[$__interval]) by (attributes_envelope, attributes_op)"
            ),
            (
                'max_over_time({service_name="unknown_service", '
                "event_name={event_name}} | json | "
                "category={category} | unwrap attributes_size_bytes "
                "[$__interval]) by (attributes_envelope, attributes_op)"
            ),
        ],
        target_names=[
            "p50 {{attributes_envelope}}/{{attributes_op}}",
            "p95 {{attributes_envelope}}/{{attributes_op}}",
            "max {{attributes_envelope}}/{{attributes_op}}",
        ],
        output=["grafana"],
        panel_id=54,
        section="Gateway & execution",
        order=12,
    )
)

core_metrics.register_core_metric(
    MetricSpec(
        name="ava_obs_exec_envelope_serialize",
        title="Exec envelope serialize (ms)",
        description=(
            "Exec envelope serialization cost — milliseconds spent "
            "serializing or parsing one envelope, unwrapped from the "
            "exec_envelope event's serialize_ms attribute (measured around "
            "the transfer in _log_envelope_transfer). avg/p95/max, grouped "
            "by envelope/op; a write-side step is a dump-cost regression, a "
            "read-side one a parse-cost regression. "
            "event_name='exec_envelope', category='telemetry'."
        ),
        event_name="exec_envelope",
        category="telemetry",
        unit="ms",
        panel="timeseries",
        query_type="logql",
        query=(
            'avg_over_time({service_name="unknown_service", '
            "event_name={event_name}} | json | "
            "category={category} | unwrap attributes_serialize_ms "
            "[$__interval]) by (attributes_envelope, attributes_op)"
        ),
        targets=[
            (
                'quantile_over_time(0.95, {service_name="unknown_service", '
                "event_name={event_name}} | json | "
                "category={category} | unwrap attributes_serialize_ms "
                "[$__interval]) by (attributes_envelope, attributes_op)"
            ),
            (
                'max_over_time({service_name="unknown_service", '
                "event_name={event_name}} | json | "
                "category={category} | unwrap attributes_serialize_ms "
                "[$__interval]) by (attributes_envelope, attributes_op)"
            ),
        ],
        target_names=[
            "avg {{attributes_envelope}}/{{attributes_op}}",
            "p95 {{attributes_envelope}}/{{attributes_op}}",
            "max {{attributes_envelope}}/{{attributes_op}}",
        ],
        output=["grafana"],
        panel_id=55,
        section="Gateway & execution",
        order=13,
    )
)
