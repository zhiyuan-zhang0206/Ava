"""Core event-stream panels — the Events trio and the gateway sample count.

The last Loki panels of the hand-written board that live outside the
``ava_observability`` pack (task #3697 S2): the three Events panels at the top
of the core section — the filtered "what happened" tier view, the per-type
count table, and the raw stream — plus the gateway-latency sample-count
companion of the route latency panel.

The trio are *views*, not metrics: their queries render verbatim from the
provisioning file, and the ``logs`` panel type carries no field config. The
``event_name``/``category`` metadata is descriptive — a raw view defines its
own predicate, which the LogQL validator permits for ``logs`` panels.

"""

from __future__ import annotations

from shared.metrics.core import core_metrics
from shared.plugin_metrics import MetricSpec

core_metrics.register_core_metric(
    MetricSpec(
        name="core_events_what_happened",
        title="Events — What happened (T0+T1)",
        description=(
            "Business audit facts and anomaly rows (exact tier predicate: warnings+ always, "
            "audit rows, and declared-anomaly names at info level). JSON parse failures "
            "excluded."
        ),
        event_name="log",
        category="log",
        panel="logs",
        query_type="logql",
        query=(
            '{service_name="unknown_service"} | json | __error__="" | '
            '((level!~"warning|error|critical" and category="audit") or '
            '(level=~"warning|error|critical" or (level!~"warning|error|critical" and '
            'category!="audit" and '
            'event_name=~"checkpoint_write_failed|claim_cas_lost|claim_cas_lost_exit'
            "|compact_turn_aborted|dangling_tool_pairing_repaired|db_outage_pause"
            "|db_outage_reconcile_retry|db_outage_wait|db_pool_acquire_slow"
            "|db_pool_acquire_timeout|db_recovered|delivery_stalled|editable_pth_repaired"
            "|error_resolved|event_log_drop|exec\\\\(cancelled\\\\)|exec\\\\(failed\\\\)"
            "|exec\\\\(thread\\\\-stuck\\\\)|exec\\\\(timeout\\\\)|exec_cancelled|exec_failed"
            "|exec_node_timeout|exec_subprocess_killed|exec_timeout|host_dispatcher_bad_channel"
            "|host_turn_crashed|host_turn_uncancellable|idle_cas_lost|label_generate_failed"
            "|launch_confirm_failed|launch_confirm_task_crashed|launch_force_terminated"
            "|llm_cancelled|llm_provider_error|llm_turn_aborted|loki_query_failed"
            "|page_restore_failed|page_restore_query_failed|page_serve_dir_missing"
            "|pgbouncer_repaired|screen_capture_notify_failed|sse_drop|stream_overloaded_retry"
            '|stream_stall_pair_terminated|stream_stalled_retry|warning_resolved")))'
        ),
        target_names=["events"],
        width=24,
        height=7,
        panel_id=2201,
        section="core",
        order=20,
    )
)


core_metrics.register_core_metric(
    MetricSpec(
        name="core_events_types",
        title="Events — types",
        description=(
            "Event counts by name, level, and category over the selected dashboard window. Tier "
            "is derived by the events API and is not a Loki label, so this query groups only "
            "fields Loki stores."
        ),
        event_name="log",
        category="log",
        panel="table",
        query_type="logql",
        query=(
            "sum by (event_name, level, category) "
            '(count_over_time({service_name="unknown_service"} | json | __error__="" '
            "[$__range]))"
        ),
        target_names=["events"],
        width=24,
        height=7,
        panel_id=2202,
        section="core",
        order=21,
    )
)


core_metrics.register_core_metric(
    MetricSpec(
        name="core_events_raw_stream",
        title="Events — raw stream (all, incl. noise)",
        description=(
            "Raw stream for debugging; most rows are internal telemetry. JSON parse failures "
            "are excluded."
        ),
        event_name="log",
        category="log",
        panel="logs",
        query_type="logql",
        query='{service_name="unknown_service"} | json | __error__=""',
        target_names=["events"],
        width=24,
        height=10,
        panel_id=2203,
        section="core",
        order=22,
    )
)


core_metrics.register_core_metric(
    MetricSpec(
        name="core_gateway_latency_sample_count",
        title="Gateway latency sample count by route",
        event_name="gateway_latency",
        category="telemetry",
        panel="timeseries",
        query_type="logql",
        query=(
            'max by (attributes_route) (max_over_time({service_name="unknown_service", '
            'event_name={event_name}} | json | category=~"{category_re}|log" | unwrap '
            "attributes_count [1m]))"
        ),
        target_names=["{{attributes_route}}"],
        panel_id=21,
        section="Gateway & execution",
        order=3,
    )
)
