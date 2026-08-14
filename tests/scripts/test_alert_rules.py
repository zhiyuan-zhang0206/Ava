"""Validation for dashboards/ops/alerts/rules.yml — the Grafana alert rules.

The rules moved from the retired Postgres events read path to the LGTM read
side (Task #1224): R1-R3, R5-R7 query Loki (the events stream as OTLP logs
under {service_name="unknown_service"}; `| json` flattens each line to
labels), R4 queries Prometheus (the ava_llm_usage_latency_ms histogram).
Keeps the rules in sync with the emitter's LogQL/OTLP contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_RULES = (
    Path(__file__).resolve().parent.parent.parent / "dashboards" / "ops" / "alerts" / "rules.yml"
)

_EXPECTED_UIDS = {
    "ava-ops-warning-error-spike",
    "ava-ops-sse-drop-backlog",
    "ava-ops-agent-restart-spike",
    "ava-ops-llm-latency-p95",
    "ava-ops-delivery-stalled-backlog",
    "ava-ops-events-freshness",
    "ava-ops-trace-disk-watermark",
}


def _load_rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    assert doc is not None
    groups = doc["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["name"] == "ava-ops"
    assert group["folder"] == "Ava"
    return group["rules"]


def _exprs(rule: dict[str, Any], datasource_uid: str) -> list[str]:
    """The expr strings of one rule's datasource queries (loki/prometheus)."""
    return [
        d["model"]["expr"]
        for d in rule["data"]
        if d.get("datasourceUid") == datasource_uid and d.get("queryType") == ""
    ]


def _threshold_params(rule: dict[str, Any]) -> list[list[Any]]:
    return [
        d["model"]["conditions"][0]["evaluator"]["params"]
        for d in rule["data"]
        if d.get("refId") == "D"
    ]


def test_seven_rules_with_expected_uids() -> None:
    rules = _load_rules()
    assert {r["uid"] for r in rules} == _EXPECTED_UIDS


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
    """OTel-detected labels are structured metadata, not stream labels —
    {…} selectors cannot match them (verified live), so every Loki rule
    must run `| json` before any event-field filter."""
    for rule in _load_rules():
        if rule["uid"] == "ava-ops-events-freshness":
            continue  # R6 is a whole-stream probe (absent_over_time), no field filters
        for expr in _exprs(rule, "loki"):
            assert "| json" in expr, f"{rule['uid']}: no | json stage:\n{expr}"
            assert "unknown_service" in expr, f"{rule['uid']}: wrong stream selector"


@pytest.mark.parametrize(
    ("uid", "event_filter"),
    [
        ("ava-ops-sse-drop-backlog", 'event_name=~"sse_drop|event_log_drop"'),
        ("ava-ops-agent-restart-spike", 'event_name="agent_restarted"'),
        ("ava-ops-delivery-stalled-backlog", 'event_name="delivery_stalled"'),
        ("ava-ops-trace-disk-watermark", 'event_name="trace"'),
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


def test_latency_rule_uses_prometheus_histogram() -> None:
    """R4's p95 now comes from the OTLP metric mirror: histogram_quantile
    over the ava_llm_usage_latency_ms histogram (the same read the Ops
    panel uses), threshold 60000 ms."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-llm-latency-p95"], "prometheus")
    assert len(exprs) == 1
    expr = exprs[0]
    assert "histogram_quantile(0.95" in expr
    assert "ava_llm_usage_latency_ms_milliseconds_bucket" in expr
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


def test_delivery_stalled_rule_filters_fresh_by_age() -> None:
    """R5's "fresh" discriminator is attributes.age_s < 600 — a numeric
    comparison on the json-flattened label (numbers parse as numbers)."""
    rules = {r["uid"]: r for r in _load_rules()}
    exprs = _exprs(rules["ava-ops-delivery-stalled-backlog"], "loki")
    assert any("attributes_age_s < 600" in e for e in exprs)
