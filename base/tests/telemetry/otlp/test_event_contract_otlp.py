from __future__ import annotations


def test_gateway_observability_payloads_and_gauge_dispositions() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("sse") == ("mode", "active_connections", "opened", "closed")
    assert payload_keys("gateway_process") == ("cpu_percent", "rss_bytes", "fd_count")
    assert payload_keys("gateway_event_loop") == ("lag_ms", "slow_ticks")
    assert {
        key: METRIC_DISPOSITION[key]
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
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("checkpoint_table_sizes") == (
        "blobs_bytes",
        "checkpoints_bytes",
        "writes_bytes",
        "blobs_live",
        "checkpoints_live",
        "writes_live",
    )
    assert {
        key: METRIC_DISPOSITION[key]
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
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

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
        assert METRIC_DISPOSITION[("pr_flow_daily", field)] == "gauge", field
    assert METRIC_DISPOSITION[("pr_flow_run", "queue_depth")] == "gauge"


def test_agent_registry_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("agent_registry") == ("max_id",)
    assert METRIC_DISPOSITION[("agent_registry", "max_id")] == "gauge"


def test_memory_search_stats_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("memory_search_stats") == ("rows", "last_save_seconds")
    assert METRIC_DISPOSITION[("memory_search_stats", "rows")] == "gauge"
    assert METRIC_DISPOSITION[("memory_search_stats", "last_save_seconds")] == "gauge"


def test_root_health_tick_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("root_health_tick") == ("home_id", "last_tick_timestamp_seconds")
    assert METRIC_DISPOSITION[("root_health_tick", "last_tick_timestamp_seconds")] == "gauge"
