"""Validation for deploy/lgtm/config/grafana/provisioning/alerting/rules.yml — the Grafana alert rules.

The two rule groups moved from the retired Postgres events read path to the
LGTM read side (Task #1224): R1-R3, R5-R7 query Loki (the events stream as
OTLP logs under {service_name="unknown_service"} with event_name/agent_id
promoted to stream labels since the 2026-08-23 cutover (Task #1467); event
filters live in the stream selector, `| json` flattens each line for the
level/category/attributes filters). R4, the gateway-metrics silence rule, and
the watchdog-tick staleness rule query Prometheus.
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
    "ava-ops-llm-rate-limit",
    "ava-ops-sse-drop-backlog",
    "ava-ops-agent-restart-spike",
    "ava-ops-llm-latency-p95",
    "ava-ops-delivery-stalled-backlog",
    "ava-ops-events-freshness",
    "ava-ops-events-low-water",
    "ava-ops-gateway-metrics-silent",
    "ava-ops-watchdog-tick-stale",
    "ava-ops-checkpoint-blobs-warning",
    "ava-ops-checkpoint-blobs-error",
    "ava-ops-trace-disk-watermark",
    "ava-ops-llm-billing-quota",
    # slow-request layer (task #1399) — user-visible latency, warning-first
    "ava-ops-gateway-latency-route-warning",
    "ava-ops-gateway-latency-route-error",
    "ava-ops-turn-duration-p95",
    "ava-ops-gw-latency-slow-warning",
    "ava-ops-gw-latency-slow-error",
    # infrastructure layer (issue #46) — the sidecar's own scrapes
    "ava-ops-host-cpu-saturated",
    "ava-ops-host-memory-pressure",
    "ava-ops-host-disk-watermark",
    "ava-ops-host-disk-watermark-93",
    "ava-ops-host-disk-watermark-95",
    "ava-ops-pg-connection-saturation",
    "ava-ops-redis-memory",
    # collector delivery layer
    "ava-ops-otelcol-queue-pressure",
    "ava-ops-otelcol-enqueue-failures",
    "ava-ops-otelcol-host-silent",
    # memory-search growth layer (task #2088/#2090) — OTLP gauge mirror
    "ava-ops-memory-search-rows-warning",
    "ava-ops-memory-search-rows-critical",
    # recovery posture — scheduled-proof failure and remote retention growth
    "ava-ops-recovery-drill-failed",
    "ava-ops-pitr-storage-growth",
}

# The infra rules and the metric each one is built on. A rename on the
# collector side (a receiver swap, a unit change that moves the Prometheus
# suffix) silently turns a rule into a permanent NoData — which is invisible,
# because noDataState is OK by design.
_INFRA_RULE_METRICS = {
    "ava-ops-host-cpu-saturated": "system_cpu_utilization_ratio",
    "ava-ops-host-memory-pressure": "system_memory_utilization_ratio",
    "ava-ops-host-disk-watermark": "system_filesystem_utilization_ratio",
    "ava-ops-host-disk-watermark-93": "system_filesystem_utilization_ratio",
    "ava-ops-host-disk-watermark-95": "system_filesystem_utilization_ratio",
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
    assert [len(group["rules"]) for group in groups] == [24, 9]
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


def test_rule_uids_fit_grafana_40_char_limit() -> None:
    """Grafana 13.1.3 rejects alert-rule UIDs longer than 40 chars and the
    whole provisioning file fails to load — every rule disappears on the next
    restart. 2026-08-25 incident: `ava-ops-gateway-latency-route-slow-warning`
    (42 chars) broke Grafana startup after a rollout; the two slow-route UIDs
    were renamed to `ava-ops-gw-latency-slow-*`. This lock keeps future
    additions under the limit at review time instead of at deploy time."""
    for rule in _load_rules():
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
    ):
        assert group_for_rule[uid] == "ava-ops-slow"


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


def test_loki_rules_match_event_labels_in_the_selector() -> None:
    """Since the 2026-08-23 index-label cutover (Task #1467) event_name is a
    promoted stream label: event-scoped rules must match it inside the stream
    selector, BEFORE the `| json` stage — the pipeline stage stays for the
    level/category/attributes filters only. (Legacy chunks without the index
    labels expired at LEGACY_READ_EXPIRES_AT, so a pipeline-form event filter
    would silently match nothing.)"""
    for rule in _load_rules():
        for expr in _exprs(rule, "loki"):
            assert "| json" in expr, f"{rule['uid']}: no | json stage:\n{expr}"
            assert "unknown_service" in expr, f"{rule['uid']}: wrong stream selector"
            selector = expr.split("| json")[0]
            assert "{" in selector and "}" in selector
            pipeline = expr.split("| json")[1]
            # event_name must never be filtered after the json stage; the only
            # event_name tokens left there would be extracted-field leftovers.
            assert "event_name" not in pipeline, (
                f"{rule['uid']}: event_name filter after | json (must be a "
                f"stream-selector matcher):\n{expr}"
            )


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
        ("ava-ops-llm-rate-limit", 'event_name="llm_provider_error"'),
        ("ava-ops-llm-billing-quota", 'event_name="llm_provider_error"'),
    ],
)
def test_event_name_rules_filter_by_event_name(uid: str, event_filter: str) -> None:
    """The event_name matcher sits in the stream selector (before `| json`);
    category stays a json-stage filter (it is not a stream label)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules[uid], "loki")
    assert any(event_filter in e for e in exprs)
    assert any(event_filter in e.split("| json")[0] for e in exprs), (
        f"{uid}: event_name matcher must be a stream selector, got:\n{exprs}"
    )
    assert any('category="telemetry"' in e for e in exprs)
    assert any('category="telemetry"' in e.split("| json")[1] for e in exprs)


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


def test_events_low_water_rule_detects_partial_write_loss() -> None:
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-events-low-water"]

    assert _exprs(rule, "loki") == [
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
    assert _threshold_params(rule) == [[150]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "lt"


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


def test_watchdog_tick_staleness_tracks_each_recent_capability() -> None:
    """A live process is insufficient when its watchdog round is wedged.

    Keep the capability's ``machine`` / ``process`` dimensions through the
    historical/current set subtraction so one silent gateway or runner is
    named, while a retired capability naturally leaves the 24-hour set.
    """
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-watchdog-tick-stale"]
    expr = _exprs(rule, "prometheus")[0]
    assert "ava_watchdog_tick_last_tick_timestamp_seconds" in expr
    assert "max_over_time" in expr
    assert "[24h]" in expr
    assert "[3m]" in expr
    assert "unless on(machine, process)" in expr
    assert "max by (machine, process)" in expr
    assert _threshold_params(rule) == [[0]]
    assert rule["for"] == "0m"
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


def test_checkpoint_blobs_high_water_rules() -> None:
    """OTLP table-size gauges warn before bloat reaches the disk emergency."""
    rules = {r["uid"]: r for r in _load_rules()}
    expectations: dict[str, tuple[str, int, str]] = {
        "ava-ops-checkpoint-blobs-warning": (
            "warning",
            2684354560,
            "max(ava_checkpoint_table_sizes_blobs_bytes) > 2684354560",
        ),
        "ava-ops-checkpoint-blobs-error": (
            "error",
            4294967296,
            "max(ava_checkpoint_table_sizes_blobs_bytes) > 4294967296",
        ),
    }

    for uid, (severity, threshold, expected_expr) in expectations.items():
        rule = rules[uid]
        exprs = _exprs(rule, "prometheus")
        assert exprs == [expected_expr]
        assert _exprs(rule, "loki") == []
        assert rule["for"] == "2h"
        assert rule["noDataState"] == "OK"
        assert rule["execErrState"] == "OK"
        assert _threshold_params(rule) == [[threshold]]
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "checkpoint_blobs_physical_bytes",
            "team": "ava-ops",
        }
        description = rule["annotations"]["description"]
        assert "repack" in description
        assert "statvfs" in description
        assert "05:00-08:00" in description
        assert "force runs" in description


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
def test_disk_watermark_rule_excludes_wsl_docker_desktop_mount(uid: str) -> None:
    """The wsl machine's docker-desktop VM image is a loop device mounted
    read-only under /mnt/wsl/docker-desktop/* and reports a constant 1.0
    utilization — a by-design non-Ava asset, not disk growth. The rule must
    never alert on it; the mountpoint prefix is WSL-specific, so the matcher
    cannot hide a real volume on any other machine (task #2024)."""
    rules = {r["uid"]: r for r in _load_rules()}
    expr = _exprs(rules[uid], "prometheus")[0]
    assert 'mountpoint!~"/mnt/wsl/docker-desktop.*"' in expr
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
    expr = _exprs(rule, "loki")[0]
    assert 'event_name="gateway_latency"' in expr
    assert f'| attributes_route_class="{route_class}"' in expr
    assert "attributes_route !~" not in expr
    assert "unwrap attributes_p95_ms" in expr
    assert "max by (attributes_route)" in expr
    assert rule["for"] == "5m"
    assert rule["labels"]["notify_im"] == "false"
    assert rule["labels"]["metric"] == metric
    assert _threshold_params(rule) == [[threshold]]
    reduce_node = next(d for d in rule["data"] if d["model"].get("type") == "reduce")
    assert reduce_node["model"].get("mode") == "byLabels"
    assert "attributes_route" in reduce_node["model"].get("includeLabels", [])


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


def test_pitr_storage_growth_rule_compares_remote_bytes_week_over_week() -> None:
    rules = {r["uid"]: r for r in _load_rules()}
    rule = rules["ava-ops-pitr-storage-growth"]

    assert _exprs(rule, "prometheus") == [
        "max by (machine, backend) (ava_pitr_remote_inventory_bytes_ratio) / "
        "clamp_min(max by (machine, backend) "
        "(ava_pitr_remote_inventory_bytes_ratio offset 7d), 1)"
    ]
    assert _exprs(rule, "loki") == []
    assert rule["for"] == "1h"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert _threshold_params(rule) == [[1.25]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "ava-ops-pitr-storage-growth",
        "metric": "pitr_remote_storage_growth",
        "team": "ava-ops",
        "notify_im": "false",
    }
