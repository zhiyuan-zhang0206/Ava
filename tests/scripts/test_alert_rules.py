"""Validation for deploy/lgtm/config/grafana/provisioning/alerting/rules.yml — the Grafana alert rules.

The two rule groups moved from the retired Postgres events read path to the
LGTM read side (Task #1224): R1-R3, R5-R7 query Loki (the events stream as
OTLP logs under {service_name="unknown_service"}; `| json` flattens each line
to labels), R4 queries Prometheus (the ava_llm_usage_latency_milliseconds histogram).
Keeps the rules in sync with the emitter's LogQL/OTLP contract.

R8-R12 (issue #46) are the infrastructure layer and query a DIFFERENT
Prometheus family: series the per-machine OTel Collector sidecar scrapes
(host_metrics / postgresql / redis), labelled with both the OS `host` and Ava
roster `machine_name`; rules group by `machine_name`. The metric names asserted
below were read off a live Prometheus 3.13.2 — the OTLP-to-
Prometheus translation adds unit suffixes (`_ratio`, `_bytes`) and `_total` on
monotonic counters, so they cannot be derived from the OTLP names by eye.
R14-R16 are the collector delivery layer: current queue pressure, NEW enqueue
failures in a bounded window, and a recently-seen machine whose collector stopped
reporting. They use the collector's own Prometheus endpoint, scraped by that
same sidecar and relayed with the infrastructure pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_RULES = (
    Path(__file__).resolve().parent.parent.parent
    / "deploy"
    / "lgtm"
    / "config"
    / "grafana"
    / "provisioning"
    / "alerting"
    / "rules.yml"
)

_EXPECTED_UIDS = {
    # application layer — Loki event stream + the LLM latency histogram
    "ava-ops-warning-error-spike",
    "ava-ops-sse-drop-backlog",
    "ava-ops-agent-restart-spike",
    "ava-ops-llm-latency-p95",
    "ava-ops-delivery-stalled-backlog",
    "ava-ops-events-freshness",
    "ava-ops-gateway-metrics-silent",
    "ava-ops-trace-disk-watermark",
    "ava-ops-llm-billing-quota",
    # slow-request layer (task #1399) — user-visible latency, warning-first
    "ava-ops-gateway-latency-route-warning",
    "ava-ops-gateway-latency-route-error",
    "ava-ops-turn-duration-p95",
    # infrastructure layer (issue #46) — the sidecar's own scrapes
    "ava-ops-host-cpu-saturated",
    "ava-ops-host-memory-pressure",
    "ava-ops-host-disk-watermark",
    "ava-ops-pg-connection-saturation",
    "ava-ops-redis-memory",
    # collector delivery layer
    "ava-ops-otelcol-queue-pressure",
    "ava-ops-otelcol-enqueue-failures",
    "ava-ops-otelcol-host-silent",
}

# The infra rules and the metric each one is built on. A rename on the
# collector side (a receiver swap, a unit change that moves the Prometheus
# suffix) silently turns a rule into a permanent NoData — which is invisible,
# because noDataState is OK by design.
_INFRA_RULE_METRICS = {
    "ava-ops-host-cpu-saturated": "system_cpu_utilization_ratio",
    "ava-ops-host-memory-pressure": "system_memory_utilization_ratio",
    "ava-ops-host-disk-watermark": "system_filesystem_utilization_ratio",
    "ava-ops-pg-connection-saturation": "postgresql_backends",
    "ava-ops-redis-memory": "redis_memory_used_bytes",
}


def _load_groups() -> list[dict[str, Any]]:
    doc = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    assert doc is not None
    groups = doc["groups"]
    assert len(groups) == 2
    assert [group["name"] for group in groups] == ["ava-ops", "ava-ops-slow"]
    assert [group["folder"] for group in groups] == ["Ava", "Ava"]
    assert [group["interval"] for group in groups] == ["1m", "5m"]
    assert [len(group["rules"]) for group in groups] == [15, 5]
    return groups


def _load_rules() -> list[dict[str, Any]]:
    groups = _load_groups()
    return [rule for group in groups for rule in group["rules"]]


def _exprs(rule: dict[str, Any], datasource_uid: str) -> list[str]:
    """The expr strings of one rule's datasource queries (loki/prometheus)."""
    return [
        d["model"]["expr"]
        for d in rule["data"]
        if d.get("datasourceUid") == datasource_uid and d.get("queryType") == ""
    ]


def _threshold_params(rule: dict[str, Any]) -> list[list[Any]]:
    """Keyed on the node TYPE, not on a refId letter — the threshold node is
    not always `D` and a letter-keyed lookup silently returns nothing."""
    return [
        d["model"]["conditions"][0]["evaluator"]["params"]
        for d in rule["data"]
        if d["model"].get("type") == "threshold"
    ]


def test_rules_have_expected_uids() -> None:
    rules = _load_rules()
    assert {r["uid"] for r in rules} == _EXPECTED_UIDS


def test_low_cost_chronic_rules_use_the_slow_group() -> None:
    groups = _load_groups()
    group_for_rule = {rule["uid"]: group["name"] for group in groups for rule in group["rules"]}
    assert group_for_rule["ava-ops-trace-disk-watermark"] == "ava-ops-slow"
    assert group_for_rule["ava-ops-llm-billing-quota"] == "ava-ops-slow"


def test_rules_never_query_postgres() -> None:
    """The whole point of the migration: no rule may query the `ops` PG
    datasource — the events-table read path is retired (#1197)."""
    for rule in _load_rules():
        for d in rule["data"]:
            if d.get("datasourceUid") == "__expr__":
                continue  # reduce/math/threshold nodes carry no datasource
            assert d["datasourceUid"] in {"loki", "prometheus"}, (
                f"{rule['uid']}: still queries {d['datasourceUid']!r}"
            )
            model = d["model"]
            assert model["datasource"]["type"] == d["datasourceUid"], (
                f"{rule['uid']}: datasource type/uid mismatch"
            )


def test_severity_is_critical_warning_error() -> None:
    """severity must be one of critical/warning/error — the alert-system
    vocabulary (Task #1224): all three push to IM, no gate."""
    for rule in _load_rules():
        severity = rule["labels"].get("severity")
        assert severity in {"critical", "warning", "error"}, (
            f"{rule['uid']}: severity {severity!r} not in the new vocabulary"
        )


def test_loki_rules_pipeline_json_before_filters() -> None:
    """Legacy chunks lack the promoted event_name/agent_id stream labels,
    so rules retain the broad selector and parse JSON before field filters
    until the migration after legacy history expires."""
    for rule in _load_rules():
        if rule["uid"] == "ava-ops-events-freshness":
            continue  # R6 is a whole-stream probe (absent_over_time), no field filters
        for expr in _exprs(rule, "loki"):
            assert "| json" in expr, f"{rule['uid']}: no | json stage:\n{expr}"
            assert "unknown_service" in expr, f"{rule['uid']}: wrong stream selector"


def test_loki_rules_filter_to_prod_cluster_after_json() -> None:
    """The one LGTM host serves prod; co-located cluster events must never
    affect its alerts, including the whole-stream freshness probe."""
    for rule in _load_rules():
        for expr in _exprs(rule, "loki"):
            normalized = " ".join(expr.split())
            assert '| json | cluster=".ava"' in normalized, (
                f"{rule['uid']}: missing prod cluster filter after json:\n{expr}"
            )


@pytest.mark.parametrize(
    ("uid", "event_filter"),
    [
        ("ava-ops-sse-drop-backlog", 'event_name=~"sse_drop|event_log_drop"'),
        ("ava-ops-agent-restart-spike", 'event_name="agent_restarted"'),
        ("ava-ops-delivery-stalled-backlog", 'event_name="delivery_stalled"'),
        ("ava-ops-trace-disk-watermark", 'event_name="trace"'),
        ("ava-ops-llm-billing-quota", 'event_name="llm_provider_error"'),
    ],
)
def test_event_name_rules_filter_by_event_name(uid: str, event_filter: str) -> None:
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules[uid], "loki")
    assert any(event_filter in e for e in exprs)
    assert any('category="telemetry"' in e for e in exprs)


def test_drop_backlog_rule_unwraps_payload_n() -> None:
    """R2 must SUM payload.n — the REAL dropped-event count — not count
    report rows: the publishers report rate-limited aggregates (every
    5s / per flush) carrying the delta in n, so counting rows understates
    the backlog by the aggregation factor (audit-round2 events-obs P1; the
    retired ops_metrics rollup summed the same way)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-sse-drop-backlog"], "loki")
    assert any("unwrap attributes_n" in e for e in exprs), (
        f"R2 must unwrap attributes_n, got:\n{exprs[0]}"
    )
    assert any("sum_over_time" in e for e in exprs)


def test_level_rule_scopes_to_observability_categories() -> None:
    """R1 (level-based) must scope to telemetry+log — audit rows are info-only
    by construction and were never part of the old agent_events alert."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-warning-error-spike"], "loki")
    assert any('category=~"telemetry|log"' in e for e in exprs)
    assert any('level=~"warning|error|critical"' in e for e in exprs)
    # the spike shape: current 5m vs prior-15m, offset window for the prior
    assert any("[5m]" in e for e in exprs)
    assert any("[15m] offset 5m" in e for e in exprs)


def test_freshness_rule_is_absent_over_time() -> None:
    """R6 (meta-observability) must probe the whole stream with
    absent_over_time — the Postgres max(ts) freshness probe is retired with
    the events table, and the rule now fires when no event of ANY kind
    reached Loki in 5m (emitter stall or broken export path alike)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-events-freshness"], "loki")
    assert any("absent_over_time" in e for e in exprs)
    assert any("[5m]" in e for e in exprs)
    assert _threshold_params(rules["ava-ops-events-freshness"]) == [[0]]


def test_gateway_metrics_silence_rule_uses_heartbeat_counter() -> None:
    """The gateway-exporter blind spot is watched through Prometheus itself,
    independently of events reaching Loki."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-gateway-metrics-silent"]
    assert _exprs(rule, "prometheus") == ["absent_over_time(ava_gateway_latency_count_total[5m])"]
    assert _threshold_params(rule) == [[0]]
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


def test_latency_rule_uses_prometheus_histogram() -> None:
    """R4's p95 now comes from the OTLP metric mirror: histogram_quantile
    over the ava_llm_usage_latency_ms histogram (the same read the Ops
    panel uses), threshold 60000 ms."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-llm-latency-p95"], "prometheus")
    assert len(exprs) == 1
    expr = exprs[0]
    assert "histogram_quantile(0.95" in expr
    assert "ava_llm_usage_latency_milliseconds_bucket" in expr
    assert "[10m]" in expr
    assert _threshold_params(rules["ava-ops-llm-latency-p95"]) == [[60000]]


def test_trace_watermark_rule_filters_degradation_action() -> None:
    """R7 (trace recording auto-degrade) must key on the disk-watermark
    action, scoped to telemetry — the one event that means the observability
    pipeline silently stopped recording. Any occurrence in the trailing 24h
    is the condition (chronic-condition semantics, threshold > 0)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-trace-disk-watermark"], "loki")
    assert any('attributes_action="recording_disabled_disk_watermark"' in e for e in exprs)
    assert any('category="telemetry"' in e for e in exprs)
    assert any("[24h]" in e for e in exprs)
    assert _threshold_params(rules["ava-ops-trace-disk-watermark"]) == [[0]]


def test_billing_rule_fires_on_the_first_occurrence() -> None:
    """R13 is the one rule with no spike threshold: an out-of-credit key fails
    every turn and only a human can clear it, so `> 0` over a 15m window and
    `for: 0m` are the point of the rule, not an oversight. A threshold or a
    `for` window creeping in here would re-hide the incident R1 already hides."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-llm-billing-quota"]
    assert _threshold_params(rule) == [[0]]
    assert rule["for"] == "0m"
    assert rule["labels"]["severity"] == "critical"
    assert any("[15m]" in e for e in _exprs(rule, "loki"))


def test_billing_rule_keys_on_the_billing_flag_not_a_status_list() -> None:
    """The discriminator is the emitted `billing` verdict
    (shared/lm/errors.py's cross-provider predicate), never a status list
    re-spelled in LogQL: a provider added to that vocabulary must be covered
    here without touching this file."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules["ava-ops-llm-billing-quota"], "loki")[0]
    assert 'attributes_billing="true"' in expr
    assert "402" not in expr, "the rule must not enumerate provider statuses itself"


def test_billing_rule_names_vendor_and_model_in_the_notification() -> None:
    """The IM message is one line and has to be self-explanatory, so the alert
    instance is grouped by (vendor, model) and both are interpolated into the
    summary — a bare count would not say whose key to top up."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-llm-billing-quota"]
    assert "sum by (attributes_vendor, attributes_model)" in _exprs(rule, "loki")[0]
    summary = rule["annotations"]["summary"]
    assert "{{ $labels.attributes_vendor }}" in summary
    assert "{{ $labels.attributes_model }}" in summary
    # shared/alerts.py:notify_text truncates the summary at 200 chars; the
    # template must still say what happened once the labels expand.
    assert len(summary) <= 200, f"summary is {len(summary)} chars, IM truncates at 200"


def test_delivery_stalled_rule_filters_fresh_by_age() -> None:
    """R5's "fresh" discriminator is attributes.age_s < 600 — a numeric
    comparison on the json-flattened label (numbers parse as numbers)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-delivery-stalled-backlog"], "loki")
    assert any("attributes_age_s < 600" in e for e in exprs)


# ─── infrastructure rules (issue #46) ────────────────────────────────────────


@pytest.mark.parametrize(("uid", "metric"), sorted(_INFRA_RULE_METRICS.items()))
def test_infra_rule_queries_its_scraped_metric(uid: str, metric: str) -> None:
    """Each infra rule reads Prometheus, and reads the series name the sidecar
    actually produces."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules[uid], "prometheus")
    assert len(exprs) == 1, f"{uid}: expected exactly one Prometheus query"
    assert metric in exprs[0], f"{uid}: does not read {metric}:\n{exprs[0]}"
    assert not _exprs(rules[uid], "loki"), f"{uid}: infra rules never query Loki"


@pytest.mark.parametrize("uid", sorted(_INFRA_RULE_METRICS))
def test_infra_rule_groups_by_machine_name(uid: str) -> None:
    """Per the 2026-08-24 user ruling, aggregation keeps the Ava roster
    `machine_name`, so win and wsl remain distinct alert instances."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules[uid], "prometheus")[0]
    assert "by (machine_name" in expr, f"{uid}: aggregates away the machine name:\n{expr}"
    assert "{{ $labels.machine_name }}" in rules[uid]["annotations"]["summary"]


def test_disk_watermark_rule_is_per_mountpoint() -> None:
    """The volume is the actionable unit: pg data, the traces mirror and the
    LGTM volumes can sit on different filesystems, and a max over all of them
    would name no path to clear."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules["ava-ops-host-disk-watermark"], "prometheus")[0]
    assert "by (machine_name, mountpoint)" in expr
    assert (
        "{{ $labels.mountpoint }}" in rules["ava-ops-host-disk-watermark"]["annotations"]["summary"]
    )


def test_infra_ratio_rules_round_for_readability() -> None:
    """The value is interpolated into the summary that reaches IM, and a raw
    float renders as 0.927223987411. Rounding to 0.001 cannot flip a verdict:
    every ratio threshold is two decimals wide."""
    rules = {r["uid"]: r for r in _load_rules()}
    for uid in (
        "ava-ops-host-cpu-saturated",
        "ava-ops-host-memory-pressure",
        "ava-ops-host-disk-watermark",
        "ava-ops-pg-connection-saturation",
    ):
        expr = _exprs(rules[uid], "prometheus")[0]
        assert "round(" in expr and "0.001)" in expr, f"{uid} is unrounded:\n{expr}"
        threshold: float = _threshold_params(rules[uid])[0][0]
        assert threshold == round(threshold, 2), f"{uid}: threshold finer than the rounding"


def test_infra_rule_thresholds() -> None:
    """The shipped defaults, in one place: they are deployment-tunable rule
    config, so a change here should be a deliberate edit, not a drift."""
    rules = {r["uid"]: r for r in _load_rules()}
    assert _threshold_params(rules["ava-ops-host-cpu-saturated"]) == [[0.9]]
    assert _threshold_params(rules["ava-ops-host-memory-pressure"]) == [[0.9]]
    assert _threshold_params(rules["ava-ops-host-disk-watermark"]) == [[0.9]]
    assert _threshold_params(rules["ava-ops-pg-connection-saturation"]) == [[0.8]]
    assert _threshold_params(rules["ava-ops-redis-memory"]) == [[2147483648]]


def test_collector_queue_pressure_is_current_and_per_exporter() -> None:
    """A lifetime failure counter never resolves after recovery. Queue
    pressure must instead compare the CURRENT size/capacity gauges and retain
    both machine_name and exporter so the alert names the blocked route."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-otelcol-queue-pressure"]
    expr = _exprs(rule, "prometheus")[0]
    assert "otelcol_exporter_queue_size" in expr
    assert "otelcol_exporter_queue_capacity" in expr
    assert "by (machine_name, exporter, data_type)" in expr
    assert "increase(" not in expr
    assert _threshold_params(rule) == [[0.8]]
    assert rule["for"] == "5m"


def test_collector_enqueue_failure_rule_uses_window_delta() -> None:
    """The enqueue-failed families are process-lifetime monotonic counters;
    alerting on their absolute value would warn forever after one outage."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-otelcol-enqueue-failures"]
    expr = _exprs(rule, "prometheus")[0]
    assert "otelcol_exporter_enqueue_failed_(log_records|metric_points|spans)_total" in expr
    assert "increase(" in expr
    assert "[5m]" in expr
    assert "sum by (machine_name, exporter)" in expr
    assert _threshold_params(rule) == [[0]]


def test_collector_silence_rule_tracks_recently_seen_machines() -> None:
    """NoDataState=OK cannot detect one vanished machine by itself. Compare
    historical/current sets and retain `machine_name` per the 2026-08-24 ruling."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-otelcol-host-silent"]
    expr = _exprs(rule, "prometheus")[0]
    assert "otelcol_process_uptime_total" in expr
    assert "max_over_time" in expr
    assert "[24h]" in expr
    assert "[5m]" in expr
    assert "unless on(machine_name)" in expr
    assert "max by (machine_name)" in expr
    assert rule["for"] == "0m", "the 5m absence window is already the debounce"
    assert "{{ $labels.machine_name }}" in rule["annotations"]["summary"]


def test_every_rule_is_silent_on_no_data_and_datasource_error() -> None:
    """A backend outage during maintenance must not fire every rule at once —
    the health-probe chain covers a dead datasource, not these."""
    for rule in _load_rules():
        assert rule["noDataState"] == "OK", rule["uid"]
        assert rule["execErrState"] == "OK", rule["uid"]


def test_gateway_latency_rules_scope_to_fast_routes() -> None:
    """R17's two tiers (3s warning / 10s error) alert on UI-facing quick
    reads only: the emitter classifies LLM-bound and slow-by-design routes,
    so the query is insulated from route-list drift. Both tiers share the
    selector, differ only in threshold."""
    rules = {r["uid"]: r for r in _load_rules()}
    for uid in ("ava-ops-gateway-latency-route-warning", "ava-ops-gateway-latency-route-error"):
        rule = rules[uid]
        expr = _exprs(rule, "loki")[0]
        assert 'event_name="gateway_latency"' in expr
        assert '| attributes_route_class="fast"' in expr
        assert "attributes_route !~" not in expr
        assert "unwrap attributes_p95_ms" in expr
        assert "max by (attributes_route)" in expr
        assert rule["for"] == "5m"
        assert rule["labels"]["notify_im"] == "false"
    assert _threshold_params(rules["ava-ops-gateway-latency-route-warning"]) == [[3000]]
    assert _threshold_params(rules["ava-ops-gateway-latency-route-error"]) == [[10000]]


def test_gateway_latency_rules_keep_the_route_label() -> None:
    """The reduce node must fan out by labels (not collapse to a single
    series), or the alert instance loses attributes_route and the summary
    names no route."""
    for rule in _load_rules():
        if rule["uid"] not in (
            "ava-ops-gateway-latency-route-warning",
            "ava-ops-gateway-latency-route-error",
        ):
            continue
        reduce_nodes = [d for d in rule["data"] if d["model"].get("type") == "reduce"]
        assert reduce_nodes, rule["uid"]
        assert reduce_nodes[0]["model"].get("mode") == "byLabels"
        assert "attributes_route" in reduce_nodes[0]["model"].get("includeLabels", [])


def test_turn_duration_rule_uses_prometheus_histogram() -> None:
    """R18 reads the OTLP-mirrored turn-end histogram (ava_turn_end_duration_seconds)
    — the Loki cross-stream quantile hit the per-query series cap, so the
    turn p95 lives on Prometheus. Threshold 75s = 2x the 24h baseline p95
    (37.6s on 2026-08-23), sustained 10m, warning-first without IM."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-turn-duration-p95"]
    exprs = _exprs(rule, "prometheus")
    assert len(exprs) == 1
    assert "ava_turn_end_duration_seconds_bucket" in exprs[0]
    assert "histogram_quantile(0.95" in exprs[0]
    assert _threshold_params(rule) == [[75]]
    assert rule["for"] == "10m"
    assert rule["labels"]["notify_im"] == "false"
    assert rule["labels"]["severity"] == "warning"
