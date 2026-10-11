"""Alert rules cases: billing rule fires on the first occurrence."""

from __future__ import annotations

import pytest
import yaml

from tests.scripts.test_alert_rules import (
    _INFRA_RULE_METRICS,
    _RULES,
    _assert_latency_query_scopes_to_route_class,
    _assert_reduce_keeps_route_label,
    _exprs,
    _load_rules,
    _threshold_params,
)


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


def test_rate_limit_rule_groups_http_429s_by_provider() -> None:
    """The warning is a provider-level burst, not a page per affected model."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-llm-rate-limit"]
    expr = _exprs(rule, "loki")[0]

    assert "sum by (attributes_vendor)" in expr
    assert 'attributes_status="429"' in expr
    assert "[5m]" in expr
    assert _threshold_params(rule) == [[5]]
    assert rule["for"] == "0m"
    assert rule["labels"]["severity"] == "warning"
    assert "{{ $labels.attributes_vendor }}" in rule["annotations"]["summary"]


def test_billing_rule_keys_on_the_billing_flag_not_a_status_list() -> None:
    """The discriminator is the emitted `billing` verdict
    (base/lm/errors.py's cross-provider predicate), never a status list
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
    # base/telemetry/alerts/__init__.py:notify_text truncates the summary at 200 chars; the
    # template must still say what happened once the labels expand.
    assert len(summary) <= 200, f"summary is {len(summary)} chars, IM truncates at 200"


def test_llm_stall_pair_rule_fires_on_the_first_pair() -> None:
    """A stream+fallback double stall is the worst single-call stall shape:
    the call is terminated for the delayed stall-retry schedule. Fires on the
    first pair (threshold > 0, R13-style) over a 15m window that is the whole
    debounce (tasks #3889/#3948)."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-llm-stall-pair"]

    assert _exprs(rule, "loki") == [
        'sum(count_over_time({service_name="unknown_service", '
        'event_name="stream_stall_pair_terminated"} | json | cluster=".ava" | '
        'category="telemetry" [15m]))'
    ]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-llm-stall-pair",
        "metric": "llm_stall_pair",
        "team": "ava-ops",
    }
    assert _threshold_params(rule) == [[0]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "gt"


def test_llm_stall_burst_rule_groups_stalls_by_vendor() -> None:
    """The burst names the stalling provider: one instance per vendor, ≥5
    stalled streams in 15m — above the trailing-7d benign ceiling of 2/15m,
    inside the 2026-09-14/15 wave's 2-9/15m range (task #3948)."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-llm-stall-burst"]
    expr = _exprs(rule, "loki")[0]

    assert "sum by (attributes_vendor)" in expr
    assert 'event_name="stream_stalled_retry"' in expr
    assert "[15m]" in expr
    assert _threshold_params(rule) == [[4]]
    assert rule["for"] == "0m"
    assert rule["labels"]["severity"] == "warning"
    assert "{{ $labels.attributes_vendor }}" in rule["annotations"]["summary"]


def test_delivery_stalled_rule_filters_fresh_by_age() -> None:
    """R5's "fresh" discriminator is attributes.age_s < 600 — a numeric
    comparison on the json-flattened label (numbers parse as numbers)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-delivery-stalled-backlog"], "loki")
    assert any("attributes_age_s < 600" in e for e in exprs)


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


@pytest.mark.parametrize(
    "uid",
    [
        "ava-ops-host-disk-watermark",
        "ava-ops-host-disk-watermark-93",
        "ava-ops-host-disk-watermark-95",
    ],
)
def test_disk_watermark_rule_is_per_mountpoint(uid: str) -> None:
    """The volume is the actionable unit: pg data, the traces mirror and the
    LGTM volumes can sit on different filesystems, and a max over all of them
    would name no path to clear."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules[uid], "prometheus")[0]
    assert "by (machine_name, mountpoint)" in expr
    assert "{{ $labels.mountpoint }}" in rules[uid]["annotations"]["summary"]


@pytest.mark.parametrize(
    "uid",
    [
        "ava-ops-host-disk-watermark",
        "ava-ops-host-disk-watermark-93",
        "ava-ops-host-disk-watermark-95",
    ],
)
def test_disk_watermark_rule_excludes_non_asset_mounts(uid: str) -> None:
    """Non-asset mounts report a foreign or transient fullness and must never
    alert: the wsl machine's docker-desktop VM image (a read-only loop device
    under /mnt/wsl/docker-desktop/* reporting a constant 1.0, task #2024);
    macOS /Volumes/* (removable, external and network volumes plus mounted
    DMG installers — internal volumes live under /System/Volumes/*); and
    scratch mounts under /private/tmp/* and /private/var/folders/* (2026-09-18,
    task #3958). The Ava data plane (root volume, /System/Volumes/Data,
    /Users/*/OrbStack on macOS; / on wsl) stays under the watermark."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules[uid], "prometheus")[0]
    assert (
        'mountpoint!~"/mnt/wsl/docker-desktop.*|/Volumes/.*|/private/tmp/.*|/private/var/folders/.*"'
        in expr
    )
    # the grouping contract survives the matcher: still per-machine, per-mount
    assert "by (machine_name, mountpoint)" in expr


def test_infra_ratio_rules_round_for_readability() -> None:
    """The value is interpolated into the summary that reaches IM, and a raw
    float renders as 0.927223987411. Rounding to 0.001 cannot flip a verdict:
    every ratio threshold is two decimals wide."""
    rules = {r["uid"]: r for r in _load_rules()}
    for uid in (
        "ava-ops-host-cpu-saturated",
        "ava-ops-host-memory-pressure",
        "ava-ops-host-disk-watermark",
        "ava-ops-host-disk-watermark-93",
        "ava-ops-host-disk-watermark-95",
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
    assert _threshold_params(rules["ava-ops-host-disk-watermark-93"]) == [[0.93]]
    assert _threshold_params(rules["ava-ops-host-disk-watermark-95"]) == [[0.95]]
    assert _threshold_params(rules["ava-ops-pg-connection-saturation"]) == [[0.8]]
    assert _threshold_params(rules["ava-ops-redis-memory"]) == [[2147483648]]


def test_disk_watermark_escalation_tiers() -> None:
    rules = {r["uid"]: r for r in _load_rules()}
    baseline_expr = _exprs(rules["ava-ops-host-disk-watermark"], "prometheus")
    expectations = {
        "ava-ops-host-disk-watermark-93": ("warning", "15m"),
        "ava-ops-host-disk-watermark-95": ("critical", "5m"),
    }

    for uid, (severity, hold) in expectations.items():
        rule = rules[uid]
        assert _exprs(rule, "prometheus") == baseline_expr
        assert rule["for"] == hold
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "host_disk",
            "team": "ava-ops",
        }
        assert "{{ $labels.machine_name }}" in rule["annotations"]["summary"]
        assert "{{ $labels.mountpoint }}" in rule["annotations"]["summary"]
        assert "{{ $values.C }}" in rule["annotations"]["summary"]


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


@pytest.mark.parametrize(
    ("uid", "route_class", "threshold", "metric"),
    [
        ("ava-ops-gateway-latency-route-warning", "fast", 3000, "gateway_latency_route_p95"),
        ("ava-ops-gateway-latency-route-error", "fast", 10000, "gateway_latency_route_p95"),
        (
            "ava-ops-gw-latency-slow-warning",
            "slow",
            5000,
            "gateway_latency_route_slow_p95",
        ),
        (
            "ava-ops-gw-latency-slow-error",
            "slow",
            10000,
            "gateway_latency_route_slow_p95",
        ),
    ],
)
def test_gateway_latency_rules_scope_to_route_class(
    uid: str, route_class: str, threshold: int, metric: str
) -> None:
    """R17 and R19 keep fast/slow routes in separately calibrated tiers."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules[uid]
    _assert_latency_query_scopes_to_route_class(_exprs(rule, "loki")[0], route_class)
    assert rule["for"] == "5m"
    assert rule["labels"]["notify_im"] == "false"
    assert rule["labels"]["metric"] == metric
    assert _threshold_params(rule) == [[threshold]]
    _assert_reduce_keeps_route_label(rule)


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


def test_memory_search_rows_rules() -> None:
    """Row-growth tiers (task #2088/#2090): the store's absolute row-count
    gauge warns at the 30k soft threshold and fires critical at the 100k
    hard cap; the 50k mark is the backend-switch evaluation point (on the
    dashboard, not an alert)."""
    rules = {r["uid"]: r for r in _load_rules()}
    expectations: dict[str, tuple[str, int]] = {
        "ava-ops-memory-search-rows-warning": ("warning", 30000),
        "ava-ops-memory-search-rows-critical": ("critical", 100000),
    }
    for uid, (severity, threshold) in expectations.items():
        rule = rules[uid]
        exprs = _exprs(rule, "prometheus")
        assert exprs == [f"max(ava_memory_search_stats_rows_ratio) > {threshold}"]
        assert _exprs(rule, "loki") == []
        assert rule["for"] == "2h"
        assert rule["noDataState"] == "OK"
        assert rule["execErrState"] == "OK"
        assert _threshold_params(rule) == [[threshold]]
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "memory_search_rows",
            "team": "ava-ops",
        }
        description = rule["annotations"]["description"]
        assert "100k" in description
        if uid == "ava-ops-memory-search-rows-warning":
            assert "50k" in description


def test_recovery_drill_failure_rule_is_immediate_and_names_the_drill() -> None:
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-recovery-drill-failed"]

    assert _exprs(rule, "loki") == [
        'sum by (attributes_drill) (count_over_time({service_name="unknown_service", '
        'event_name="recovery_drill_failed"} | json | cluster=".ava" | '
        'category="telemetry" | level="error" [1h]))'
    ]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert _threshold_params(rule) == [[0]]
    assert rule["labels"] == {
        "severity": "error",
        "ruleUID": "ava-ops-recovery-drill-failed",
        "metric": "recovery_drill_failure",
        "team": "ava-ops",
    }
    assert "attributes_drill" in rule["annotations"]["summary"]


def test_tempo_backend_down_rule_tracks_the_remote_scrape() -> None:
    """The silence guard for the remote Tempo backend (task #3330): the rule
    fires on `up{job="tempo"}` below 1 held for 1h. A drift in the scrape job
    name, the comparison direction, or the labels would silently disable the
    only signal for the remote trace store - it is deliberately outside the
    station healthcheck repair loop."""
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-tempo-backend-down"]

    assert _exprs(rule, "prometheus") == ['up{job="tempo"}']
    assert _exprs(rule, "loki") == []
    assert rule["for"] == "1h"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert _threshold_params(rule) == [[1]]
    assert [
        d["model"]["conditions"][0]["evaluator"]["type"]
        for d in rule["data"]
        if d["model"].get("type") == "threshold"
    ] == ["lt"]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-tempo-backend-down",
        "metric": "tempo_up",
        "team": "ava-ops",
    }


def test_telemetry_queue_loss_is_an_immediate_error_on_independent_metrics() -> None:
    rule = next(r for r in _load_rules() if r["uid"] == "ava-ops-telemetry-queue-loss")
    assert rule["labels"]["severity"] == "error"
    assert rule["for"] == "0s"
    query = next(q for q in rule["data"] if q["refId"] == "A")
    assert query["datasourceUid"] == "prometheus"
    assert "ava_event_log_drop_last_dropped_at_ratio" in query["model"]["expr"]
    threshold = next(q for q in rule["data"] if q["refId"] == "D")
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "lt", "params": [300]}


def test_delete_rules_tombstones_never_name_live_rules() -> None:
    """The R23 tombstone must stay listed (orgId 1) and never name a live rule;
    a dropped entry silently re-orphans the retire (README "Retired rules")."""
    doc = yaml.safe_load((_RULES.parent / "delete-rules.yml").read_text(encoding="utf-8"))
    assert doc["apiVersion"] == 1
    entries = doc["deleteRules"]
    assert entries, "an empty deleteRules re-orphans every retired rule"
    live = {r["uid"] for r in _load_rules()}
    for entry in entries:
        assert set(entry) == {"orgId", "uid"} and entry["orgId"] == 1
        assert entry["uid"] and len(entry["uid"]) <= 40 and entry["uid"] not in live
    assert {"orgId": 1, "uid": "ava-ops-pitr-storage-growth"} in entries
