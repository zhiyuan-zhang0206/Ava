from __future__ import annotations

import re
import types
from pathlib import Path
from typing import Any, NotRequired, Required, Union, get_args, get_origin, get_type_hints

import pytest
import yaml

from base.events.contract import EVENTS, payload_keys, telemetry_events
from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION
from base.telemetry.otlp.telemetry_otlp_metrics import strip_unit_suffix, unit_for
from tests.scripts.test_alert_rules_signals import SIGNALS

RULES = (
    Path(__file__).resolve().parent.parent.parent
    / "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml"
)

_EXPECTED_UIDS = {
    "ava-ops-warning-error-spike",
    "ava-ops-llm-rate-limit",
    "ava-ops-sse-drop-backlog",
    "ava-ops-telemetry-queue-loss",
    "ava-ops-llm-latency-p95",
    "ava-ops-delivery-stalled-backlog",
    "ava-ops-events-freshness",
    "ava-ops-events-low-water",
    "ava-ops-gateway-metrics-silent",
    "ava-ops-root-health-stale",
    "ava-ops-checkpoint-blobs-warning",
    "ava-ops-checkpoint-blobs-error",
    "ava-ops-checkpoint-blobs-freshness",
    "ava-ops-checkpoint-blobs-growth",
    "ava-ops-trace-disk-watermark",
    "ava-ops-llm-billing-quota",
    "ava-ops-gateway-latency-route-warning",
    "ava-ops-gateway-latency-route-error",
    "ava-ops-turn-duration-p95",
    "ava-ops-gw-latency-slow-warning",
    "ava-ops-gw-latency-slow-error",
    "ava-ops-host-cpu-saturated",
    "ava-ops-host-memory-pressure",
    "ava-ops-host-disk-watermark",
    "ava-ops-host-disk-watermark-93",
    "ava-ops-host-disk-watermark-95",
    "ava-ops-pg-connection-saturation",
    "ava-ops-redis-memory",
    "ava-ops-otelcol-queue-pressure",
    "ava-ops-otelcol-enqueue-failures",
    "ava-ops-otelcol-host-silent",
    "ava-ops-prom-too-old-samples",
    "ava-ops-memory-search-rows-warning",
    "ava-ops-memory-search-rows-critical",
    "ava-ops-recovery-drill-failed",
    "ava-ops-backup-dump-failed",
    "ava-ops-tempo-backend-down",
}

INFRA_RULE_METRICS = {
    "ava-ops-host-cpu-saturated": "system_cpu_utilization_ratio",
    "ava-ops-host-memory-pressure": "system_memory_utilization_ratio",
    "ava-ops-host-disk-watermark": "system_filesystem_utilization_ratio",
    "ava-ops-host-disk-watermark-93": "system_filesystem_utilization_ratio",
    "ava-ops-host-disk-watermark-95": "system_filesystem_utilization_ratio",
    "ava-ops-pg-connection-saturation": "postgresql_backends",
    "ava-ops-redis-memory": "redis_memory_used_bytes",
}


def _load_groups() -> list[dict[str, Any]]:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    assert doc is not None
    groups = doc["groups"]
    assert len(groups) == 2
    assert [group["name"] for group in groups] == ["ava-ops", "ava-ops-slow"]
    assert [group["folder"] for group in groups] == ["Ava", "Ava"]
    assert [group["interval"] for group in groups] == ["1m", "5m"]
    assert [len(group["rules"]) for group in groups] == [52, 14]
    return groups


def load_rules() -> list[dict[str, Any]]:
    groups = _load_groups()
    return [rule for group in groups for rule in group["rules"]]


def rule_query_exprs(rule: dict[str, Any], datasource_uid: str) -> list[str]:
    return [
        d["model"]["expr"]
        for d in rule["data"]
        if d.get("datasourceUid") == datasource_uid and d.get("queryType") == ""
    ]


def threshold_params(rule: dict[str, Any]) -> list[list[Any]]:
    return [
        d["model"]["conditions"][0]["evaluator"]["params"]
        for d in rule["data"]
        if d["model"].get("type") == "threshold"
    ]


def test_rules_have_expected_uids() -> None:
    rules = load_rules()
    assert {r["uid"] for r in rules} == _EXPECTED_UIDS | set(SIGNALS)


def test_rule_uids_fit_grafana_40_char_limit() -> None:
    for rule in load_rules():
        assert len(rule["uid"]) <= 40, (
            f"uid {rule['uid']!r} is {len(rule['uid'])} chars — Grafana caps "
            "alert-rule UIDs at 40 and fails the whole provisioning file"
        )


def test_low_cost_rules_use_the_slow_group() -> None:
    groups = _load_groups()
    group_for_rule = {rule["uid"]: group["name"] for group in groups for rule in group["rules"]}
    for uid in (
        "ava-ops-trace-disk-watermark",
        "ava-ops-llm-rate-limit",
        "ava-ops-llm-billing-quota",
        "ava-ops-gateway-latency-route-warning",
        "ava-ops-gateway-latency-route-error",
        "ava-ops-turn-duration-p95",
        "ava-ops-gw-latency-slow-warning",
        "ava-ops-gw-latency-slow-error",
        "ava-ops-tempo-backend-down",
    ):
        assert group_for_rule[uid] == "ava-ops-slow"


def test_rules_never_query_postgres() -> None:
    for rule in load_rules():
        for d in rule["data"]:
            if d.get("datasourceUid") == "__expr__":
                continue
            assert d["datasourceUid"] in {"loki", "prometheus"}, (
                f"{rule['uid']}: still queries {d['datasourceUid']!r}"
            )
            model = d["model"]
            assert model["datasource"]["type"] == d["datasourceUid"], (
                f"{rule['uid']}: datasource type/uid mismatch"
            )


def test_severity_is_critical_warning_error() -> None:
    for rule in load_rules():
        severity = rule["labels"].get("severity")
        assert severity in {"critical", "warning", "error"}, (
            f"{rule['uid']}: severity {severity!r} not in the new vocabulary"
        )


def test_loki_rules_match_event_labels_in_the_selector() -> None:
    for rule in load_rules():
        for expr in rule_query_exprs(rule, "loki"):
            assert "| json" in expr, f"{rule['uid']}: no | json stage:\n{expr}"
            assert "unknown_service" in expr, f"{rule['uid']}: wrong stream selector"
            selector = expr.split("| json")[0]
            assert "{" in selector and "}" in selector
            pipeline = expr.split("| json")[1]
            assert "event_name" not in pipeline, (
                f"{rule['uid']}: event_name filter after | json (must be a "
                f"stream-selector matcher):\n{expr}"
            )


def test_loki_rules_filter_to_prod_cluster_after_json() -> None:
    for rule in load_rules():
        for expr in rule_query_exprs(rule, "loki"):
            normalized = " ".join(expr.split())
            assert '| json | cluster=".ava"' in normalized, (
                f"{rule['uid']}: missing prod cluster filter after json:\n{expr}"
            )


@pytest.mark.parametrize(
    ("uid", "event_filter"),
    [
        ("ava-ops-sse-drop-backlog", 'event_name=~"sse_drop|event_log_drop"'),
        ("ava-ops-delivery-stalled-backlog", 'event_name="delivery_stalled"'),
        ("ava-ops-trace-disk-watermark", 'event_name="trace"'),
        ("ava-ops-llm-rate-limit", 'event_name="llm_provider_error"'),
        ("ava-ops-llm-billing-quota", 'event_name="llm_provider_error"'),
    ],
)
def test_event_name_rules_filter_by_event_name(uid: str, event_filter: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules[uid], "loki")
    assert any(event_filter in e for e in exprs)
    assert any(event_filter in e.split("| json")[0] for e in exprs), (
        f"{uid}: event_name matcher must be a stream selector, got:\n{exprs}"
    )
    assert any('category="telemetry"' in e for e in exprs)
    assert any('category="telemetry"' in e.split("| json")[1] for e in exprs)


def test_drop_backlog_rule_unwraps_payload_n() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["ava-ops-sse-drop-backlog"], "loki")
    assert any("unwrap attributes_n" in e for e in exprs), (
        f"R2 must unwrap attributes_n, got:\n{exprs[0]}"
    )
    assert any("sum_over_time" in e for e in exprs)


def test_level_rule_scopes_to_observability_categories() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["ava-ops-warning-error-spike"], "loki")
    assert any('category=~"telemetry|log"' in e for e in exprs)
    assert any('level=~"warning|error|critical"' in e for e in exprs)
    assert any("[5m]" in e for e in exprs)
    assert any("[15m] offset 5m" in e for e in exprs)


def test_freshness_rule_is_absent_over_time() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["ava-ops-events-freshness"], "loki")
    assert any("absent_over_time" in e for e in exprs)
    assert any("[5m]" in e for e in exprs)
    assert threshold_params(rules["ava-ops-events-freshness"]) == [[0]]


def test_events_low_water_rule_detects_partial_write_loss() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["ava-ops-events-low-water"]

    assert rule_query_exprs(rule, "loki") == [
        'sum(count_over_time({service_name="unknown_service"} | json | '
        'cluster=".ava" [5m])) or vector(0)'
    ]
    assert rule["for"] == "10m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-events-low-water",
        "metric": "events_freshness",
        "team": "ava-ops",
    }
    assert threshold_params(rule) == [[150]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "lt"


def test_gateway_metrics_silence_rule_uses_heartbeat_counter() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["ava-ops-gateway-metrics-silent"]
    assert rule_query_exprs(rule, "prometheus") == [
        "absent_over_time(ava_gateway_latency_count_total[5m])"
    ]
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


def test_root_health_tick_staleness_tracks_each_recent_root_home() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["ava-ops-root-health-stale"]
    expr = rule_query_exprs(rule, "prometheus")[0]
    assert "ava_root_health_tick_last_tick_timestamp_seconds" in expr
    assert "max_over_time" in expr
    assert "ava_root_health_expected_expected_since_timestamp_seconds[24h]" in expr
    assert "[3m]" in expr
    assert "unless on(machine, home_id, process)" in expr
    assert "max by (machine, home_id, process)" in expr
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


def test_latency_rule_uses_prometheus_histogram() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["ava-ops-llm-latency-p95"], "prometheus")
    assert len(exprs) == 1
    expr = exprs[0]
    assert "histogram_quantile(0.95" in expr
    assert "ava_llm_usage_latency_milliseconds_bucket" in expr
    assert "[10m]" in expr
    assert threshold_params(rules["ava-ops-llm-latency-p95"]) == [[60000]]


def test_checkpoint_blobs_high_water_rules() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expectations: dict[str, tuple[str, int, str]] = {
        "ava-ops-checkpoint-blobs-warning": (
            "warning",
            2684354560,
            "max(ava_checkpoint_table_sizes_blobs_bytes_ratio) > 2684354560",
        ),
        "ava-ops-checkpoint-blobs-error": (
            "error",
            4294967296,
            "max(ava_checkpoint_table_sizes_blobs_bytes_ratio) > 4294967296",
        ),
    }

    for uid, (severity, threshold, expected_expr) in expectations.items():
        rule = rules[uid]
        exprs = rule_query_exprs(rule, "prometheus")
        assert exprs == [expected_expr]
        assert rule_query_exprs(rule, "loki") == []
        assert rule["for"] == "2h"
        assert rule["noDataState"] == "OK"
        assert rule["execErrState"] == "OK"
        assert threshold_params(rule) == [[threshold]]
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "checkpoint_blobs_physical_bytes",
            "team": "ava-ops",
        }
        description = rule["annotations"]["description"]
        assert "repack" in description
        assert "statvfs" in description
        assert "hourly" in description
        assert "vacuum run" in description


def test_checkpoint_blobs_freshness_guard() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["ava-ops-checkpoint-blobs-freshness"]
    exprs = rule_query_exprs(rule, "prometheus")
    assert exprs == ["absent_over_time(ava_checkpoint_table_sizes_blobs_bytes_ratio[2h])"]
    assert rule_query_exprs(rule, "loki") == []
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[0]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-checkpoint-blobs-freshness",
        "metric": "checkpoint_blobs_freshness",
        "team": "ava-ops",
    }
    description = rule["annotations"]["description"]
    assert "events_maintenance" in description
    assert "blind" in description


def test_checkpoint_blobs_growth_tier() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["ava-ops-checkpoint-blobs-growth"]
    exprs = rule_query_exprs(rule, "prometheus")
    assert exprs == [
        "max(ava_checkpoint_table_sizes_blobs_bytes_ratio) - "
        "max(ava_checkpoint_table_sizes_blobs_bytes_ratio offset 6h)"
    ]
    assert rule_query_exprs(rule, "loki") == []
    assert rule["for"] == "1h"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[1073741824]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-checkpoint-blobs-growth",
        "metric": "checkpoint_blobs_growth",
        "team": "ava-ops",
    }
    description = rule["annotations"]["description"]
    assert "2026-09-13" in description
    assert "repack" in description


_UNIT_RENDER = {"1": "_ratio", "ms": "_milliseconds", "s": "_seconds"}
_KIND_EXTENSIONS = {
    "counter": ("_total",),
    "histogram": ("_bucket", "_count", "_sum"),
}

_RUNTIME_KIND_FIELDS: dict[tuple[str, str], str | None] = {
    ("delivery_stalled", "age_s"): "counter",
    ("shell_ttl_renewed", "ttl_s"): "counter",
    ("heartbeat_paused", "duration_s"): "counter",
}

_UNLISTED_METRIC_NAMES: dict[str, str] = {}


def _strip_optional(hint: Any) -> Any:
    while True:
        origin = get_origin(hint)
        if origin in (NotRequired, Required):
            hint = get_args(hint)[0]
            continue
        if origin in (Union, types.UnionType):
            args = [arg for arg in get_args(hint) if arg is not type(None)]
            if len(args) == 1:
                hint = args[0]
                continue
        return hint


def _metric_kind(event: str, field: str) -> str | None:
    override = METRIC_DISPOSITION.get((event, field), "default")
    if override != "default":
        return override
    if (event, field) in _RUNTIME_KIND_FIELDS:
        return _RUNTIME_KIND_FIELDS[(event, field)]
    spec = EVENTS[event]
    if spec.payload is None:
        return None
    hint = get_type_hints(spec.payload).get(field)
    if hint is None:
        return None
    hint = _strip_optional(hint)
    if hint is float:
        return "histogram"
    if hint is int:
        return "counter"
    return None


def _rendered_names(event: str, field: str) -> set[str]:
    kind = _metric_kind(event, field)
    if kind is None:
        return set()
    base = f"ava_{event}_{strip_unit_suffix(field)}"
    unit = unit_for(field)
    if kind == "gauge":
        return {base + _UNIT_RENDER[unit]}
    suffix = "" if unit == "1" else _UNIT_RENDER[unit]
    if kind == "counter":
        name = base + suffix
        return {name if name.endswith("_total") else name + "_total"}
    return {base + suffix + extension for extension in _KIND_EXTENSIONS[kind]}


def _emitted_metric_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for event in sorted(telemetry_events()):
        for field in payload_keys(event):
            for name in _rendered_names(event, field):
                names[name] = f"{event}.{field}"
    return names


def _rule_metric_tokens() -> set[str]:
    tokens: set[str] = set()
    for rule in load_rules():
        for query in rule["data"]:
            expr = query["model"].get("expr")
            if isinstance(expr, str):
                tokens.update(re.findall(r"ava_[a-z0-9_]+", expr))
    return tokens


def test_rule_metric_references_match_the_prometheus_translation() -> None:
    emitted = _emitted_metric_names()
    unknown: list[str] = []
    for token in sorted(_rule_metric_tokens()):
        if token in emitted or token in _UNLISTED_METRIC_NAMES:
            continue
        near = sorted(name for name in emitted if name.startswith(token) or token.startswith(name))[
            :3
        ]
        unknown.append(f"{token} (nearest emitted: {near})")
    assert not unknown, "rule metric names the translation cannot render: " + "; ".join(unknown)


def test_unit_one_gauges_render_with_the_ratio_suffix() -> None:
    assert _rendered_names("checkpoint_table_sizes", "blobs_bytes") == {
        "ava_checkpoint_table_sizes_blobs_bytes_ratio"
    }
    assert _rendered_names("event_log_drop", "last_dropped_at") == {
        "ava_event_log_drop_last_dropped_at_ratio"
    }
    assert _rendered_names("memory_search_stats", "rows") == {"ava_memory_search_stats_rows_ratio"}
    assert _rendered_names("gateway_latency", "count") == {"ava_gateway_latency_count_total"}
    assert _rendered_names("turn_end", "duration_seconds") == {
        "ava_turn_end_duration_seconds_bucket",
        "ava_turn_end_duration_seconds_count",
        "ava_turn_end_duration_seconds_sum",
    }


def test_runtime_typed_fields_pin_their_emitted_kind() -> None:
    assert _rendered_names("delivery_stalled", "age_s") == {"ava_delivery_stalled_age_s_total"}
    assert _rendered_names("shell_ttl_renewed", "ttl_s") == {"ava_shell_ttl_renewed_ttl_s_total"}
    assert _rendered_names("heartbeat_paused", "duration_s") == {
        "ava_heartbeat_paused_duration_s_total"
    }


def test_unlisted_metric_names_have_no_stale_entries() -> None:
    stale = sorted(set(_UNLISTED_METRIC_NAMES) - _rule_metric_tokens())
    assert not stale, f"stale _UNLISTED_METRIC_NAMES entries: {stale}"


def test_trace_watermark_rule_filters_degradation_action() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["ava-ops-trace-disk-watermark"], "loki")
    assert any('attributes_action="recording_disabled_disk_watermark"' in e for e in exprs)
    assert any('category="telemetry"' in e for e in exprs)
    assert any("[24h]" in e for e in exprs)
    assert threshold_params(rules["ava-ops-trace-disk-watermark"]) == [[0]]


def assert_latency_query_scopes_to_route_class(expr: str, route_class: str) -> None:
    assert 'event_name="gateway_latency"' in expr
    assert f'| attributes_route_class="{route_class}"' in expr
    assert "attributes_route !~" not in expr
    assert "unwrap attributes_p95_ms" in expr
    assert "max by (attributes_route)" in expr


def assert_reduce_keeps_route_label(rule: dict[str, Any]) -> None:
    reduce_node = next(d for d in rule["data"] if d["model"].get("type") == "reduce")
    assert reduce_node["model"].get("mode") == "byLabels"
    assert "attributes_route" in reduce_node["model"].get("includeLabels", [])
