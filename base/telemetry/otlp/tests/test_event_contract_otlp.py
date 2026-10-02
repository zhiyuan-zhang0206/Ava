"""The event contract's payload keys and the OTLP exporter's metric dispositions agree: gauges for absolute state, counters for interval counts."""

from __future__ import annotations


def test_gateway_observability_payloads_and_gauge_dispositions() -> None:
    """Gateway absolute state is gauged; interval counts remain counters."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("sse") == ("mode", "active_connections", "opened", "closed")
    assert payload_keys("gateway_process") == ("cpu_percent", "rss_bytes", "fd_count")
    assert payload_keys("gateway_event_loop") == ("lag_ms", "slow_ticks")
    assert {
        key: _METRIC_DISPOSITION[key]
        for key in (
            ("sse", "active_connections"),
            ("gateway_process", "cpu_percent"),
            ("gateway_process", "rss_bytes"),
            ("gateway_process", "fd_count"),
        )
    } == {
        ("sse", "active_connections"): "gauge",
        ("gateway_process", "cpu_percent"): "gauge",
        ("gateway_process", "rss_bytes"): "gauge",
        ("gateway_process", "fd_count"): "gauge",
    }


def test_checkpoint_table_sizes_payload_and_metric_disposition() -> None:
    """The hourly table-size state is emitted as six absolute gauges: three
    physical sizes plus the three live row counts (live growth vs dead-tuple
    bloat are separable in the growth curve)."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("checkpoint_table_sizes") == (
        "blobs_bytes",
        "checkpoints_bytes",
        "writes_bytes",
        "blobs_live",
        "checkpoints_live",
        "writes_live",
    )
    assert {
        key: _METRIC_DISPOSITION[key]
        for key in (
            ("checkpoint_table_sizes", "blobs_bytes"),
            ("checkpoint_table_sizes", "checkpoints_bytes"),
            ("checkpoint_table_sizes", "writes_bytes"),
            ("checkpoint_table_sizes", "blobs_live"),
            ("checkpoint_table_sizes", "checkpoints_live"),
            ("checkpoint_table_sizes", "writes_live"),
        )
    } == {
        ("checkpoint_table_sizes", "blobs_bytes"): "gauge",
        ("checkpoint_table_sizes", "checkpoints_bytes"): "gauge",
        ("checkpoint_table_sizes", "writes_bytes"): "gauge",
        ("checkpoint_table_sizes", "blobs_live"): "gauge",
        ("checkpoint_table_sizes", "checkpoints_live"): "gauge",
        ("checkpoint_table_sizes", "writes_live"): "gauge",
    }


def test_pr_flow_payloads_and_metric_dispositions() -> None:
    """The PR-flow sampler re-emits its whole trailing window every run, so
    every numeric field is per-day absolute state: an int would default to a
    Counter and accrue across re-emissions, a float to a Histogram — each
    must be dispositioned as a gauge (task #2139)."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("pr_flow_daily") == (
        "day",
        "merged_count",
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
        "flake_new_quarantines",
    )
    assert payload_keys("pr_flow_run") == ("queue_depth",)
    for field in (
        "merged_count",
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
        "flake_new_quarantines",
    ):
        assert _METRIC_DISPOSITION[("pr_flow_daily", field)] == "gauge", field
    assert _METRIC_DISPOSITION[("pr_flow_run", "queue_depth")] == "gauge"


def test_agent_registry_payload_and_metric_disposition() -> None:
    """The 60s agent-registry max-id sample is absolute state, not a sum:
    declared as an int payload (the event contract), dispositioned as a
    gauge (task #2010) — an int would otherwise default to a Counter and
    accrue value on every sample."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("agent_registry") == ("max_id",)
    assert _METRIC_DISPOSITION[("agent_registry", "max_id")] == "gauge"


def test_memory_search_stats_payload_and_metric_disposition() -> None:
    """The 60s memory-search store sample is absolute state, not a sum:
    rows is an int that would otherwise default to a Counter and accrue on
    every sample; last_save_seconds a float that would default to a
    Histogram. Both must be declared gauges (task #2088)."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("memory_search_stats") == ("rows", "last_save_seconds")
    assert _METRIC_DISPOSITION[("memory_search_stats", "rows")] == "gauge"
    assert _METRIC_DISPOSITION[("memory_search_stats", "last_save_seconds")] == "gauge"


def test_root_health_tick_payload_and_metric_disposition() -> None:
    """A completed watchdog round publishes its wall-clock timestamp as a
    gauge, so Prometheus exposes freshness rather than a meaningless sum."""
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("root_health_tick") == ("home_id", "last_tick_timestamp_seconds")
    assert _METRIC_DISPOSITION[("root_health_tick", "last_tick_timestamp_seconds")] == "gauge"
