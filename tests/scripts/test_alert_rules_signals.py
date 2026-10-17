from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from base.events.contract import EVENTS, payload_keys

_RULES = (
    Path(__file__).resolve().parents[2]
    / "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml"
)

_ENVELOPE = frozenset({"agent_id", "machine", "cluster", "category", "level", "process", "source"})


class Signal(NamedTuple):
    event: str
    group: str
    pending: str
    severity: str
    threshold: float = 0


SIGNALS: dict[str, Signal] = {
    "meiva-ops-health-probe-warning": Signal("health_probe_failing", "meiva-ops", "3m", "warning"),
    "meiva-ops-health-probe-error": Signal("health_probe_failing", "meiva-ops", "10m", "error"),
    "meiva-ops-health-probe-silent": Signal("health_probe_ran", "meiva-ops", "0m", "error"),
    "meiva-ops-service-start-unready": Signal(
        "service_start_unready", "meiva-ops", "0m", "warning"
    ),
    "meiva-ops-boot-retries-exhausted": Signal(
        "boot_retries_exhausted", "meiva-ops", "0m", "error"
    ),
    "meiva-ops-schedule-verify-failed": Signal(
        "schedule_verify_failed", "meiva-ops", "0m", "error"
    ),
    "meiva-ops-schedule-stalled": Signal("schedule_stalled", "meiva-ops-slow", "0m", "warning"),
    "meiva-ops-schedule-convergence-abandoned": Signal(
        "schedule_convergence_abandoned", "meiva-ops-slow", "0m", "warning"
    ),
    "meiva-ops-maintenance-hold-overdue": Signal(
        "maintenance_hold_overdue", "meiva-ops-slow", "0m", "warning"
    ),
    "meiva-ops-agent-continuation-lost": Signal(
        "agent_continuation_lost", "meiva-ops", "0m", "error"
    ),
    "meiva-ops-machine-offline-warning": Signal(
        "machine_probe_failed", "meiva-ops", "3m", "warning"
    ),
    "meiva-ops-machine-offline-error": Signal("machine_probe_failed", "meiva-ops", "10m", "error"),
    "meiva-ops-exec-child-boot-failed": Signal(
        "exec_child_boot_failed", "meiva-ops", "0s", "warning"
    ),
    "meiva-ops-inspect-metrics-gap": Signal(
        "inspect_metrics_coverage_gap", "meiva-ops", "0s", "warning"
    ),
    "meiva-ops-impersonation-seal-stuck": Signal(
        "impersonation_event_log_incomplete", "meiva-ops", "0s", "warning"
    ),
    "meiva-ops-impersonation-capture-failed": Signal(
        "impersonation_event_log_incomplete", "meiva-ops", "0s", "warning"
    ),
    "meiva-ops-impersonation-notice-failed": Signal(
        "impersonation_terminal_notice_failed", "meiva-ops", "0m", "warning"
    ),
    "meiva-ops-im-push-failed": Signal("im_push_failed", "meiva-ops", "0s", "warning"),
    "meiva-ops-im-feishu-owner-seed-failed": Signal(
        "im_feishu_owner_seed_failed", "meiva-ops", "0s", "warning"
    ),
    "meiva-ops-im-push-stall": Signal("im_push_health", "meiva-ops", "2m", "warning"),
    "meiva-ops-lifecycle-pointer-stuck": Signal(
        "lifecycle_pointer_stuck", "meiva-ops-slow", "0s", "warning", 0
    ),
    "meiva-ops-delivery-stalled": Signal("delivery_stalled", "meiva-ops", "1m", "warning"),
    "meiva-ops-db-pool-acquire-slow": Signal("db_pool_acquire_slow", "meiva-ops", "2m", "warning"),
    "meiva-ops-root-unit-restart-failed": Signal(
        "root_unit_failure_state", "meiva-ops", "0m", "error"
    ),
    "meiva-ops-root-unit-breaker-open": Signal(
        "root_unit_failure_state", "meiva-ops", "0m", "critical"
    ),
    "meiva-ops-root-unit-not-revivable": Signal(
        "root_unit_not_revivable", "meiva-ops", "0m", "error", 1
    ),
    "meiva-ops-root-diagnostic-venv": Signal("root_diagnostic", "meiva-ops", "0m", "warning", 1),
    "meiva-ops-root-diagnostic-brew-pin": Signal(
        "root_diagnostic", "meiva-ops", "0m", "warning", 1
    ),
    "meiva-ops-root-diagnostic-browser-reach": Signal(
        "root_diagnostic", "meiva-ops-slow", "0m", "warning", 2
    ),
}


def _rules() -> dict[str, tuple[str, dict[str, Any]]]:
    document: dict[str, Any] = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    return {
        rule["uid"]: (group["name"], rule)
        for group in document["groups"]
        for rule in group["rules"]
    }


def _query(rule: dict[str, Any]) -> str:
    (expr,) = [d["model"]["expr"] for d in rule["data"] if d["refId"] == "A"]
    return " ".join(str(expr).split())


def _threshold(rule: dict[str, Any]) -> float:
    (node,) = [d["model"] for d in rule["data"] if d["refId"] == "D"]
    (params,) = [c["evaluator"]["params"] for c in node["conditions"]]
    return params[0]


@pytest.mark.parametrize("uid", sorted(SIGNALS))
def test_the_rule_carries_the_pinned_debounce_severity_and_threshold(uid: str) -> None:
    signal = SIGNALS[uid]
    group, rule = _rules()[uid]
    assert group == signal.group
    assert rule["for"] == signal.pending
    assert rule["labels"]["severity"] == signal.severity
    assert _threshold(rule) == signal.threshold
    assert rule["labels"]["ruleUID"] == uid
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


@pytest.mark.parametrize("uid", sorted(SIGNALS))
def test_the_rule_reads_its_declared_telemetry_event_in_the_prod_cluster(uid: str) -> None:
    query = _query(_rules()[uid][1])
    assert f'event_name="{SIGNALS[uid].event}"' in query
    spec = EVENTS[SIGNALS[uid].event]
    assert spec.category == "telemetry"
    assert '| json | cluster=".meiva"' in query
    assert 'category="telemetry"' in query


@pytest.mark.parametrize("uid", sorted(SIGNALS))
def test_the_rule_only_groups_and_filters_by_fields_the_event_carries(uid: str) -> None:
    signal = SIGNALS[uid]
    query = _query(_rules()[uid][1])
    declared = set(payload_keys(signal.event))
    used = set(re.findall(r"attributes_(\w+)", query))
    assert used <= declared, f"{uid} reads {used - declared}, not in {signal.event}'s payload"
    grouped = re.findall(r"by \(([^)]*)\)", query)
    for label in (name.strip() for part in grouped for name in part.split(",")):
        assert label in _ENVELOPE or label.removeprefix("attributes_") in declared


@pytest.mark.parametrize("uid", sorted(SIGNALS))
def test_the_notification_text_names_only_labels_the_query_keeps(uid: str) -> None:
    rule = _rules()[uid][1]
    grouped = {
        name.strip()
        for part in re.findall(r"by \(([^)]*)\)", _query(rule))
        for name in part.split(",")
    }
    text = rule["annotations"]["summary"] + rule["annotations"]["description"]
    for label in re.findall(r"\$labels\.(\w+)", text):
        assert label in grouped, f"{uid} names {{{{ $labels.{label} }}}} but groups by {grouped}"


def test_the_health_rules_label_their_check_for_routing_and_the_window_silence() -> None:
    for uid in ("meiva-ops-health-probe-warning", "meiva-ops-health-probe-error"):
        assert "sum by (attributes_check)" in _query(_rules()[uid][1])


def test_the_probe_dead_man_fires_on_an_absent_heartbeat_and_routes_to_telegram() -> None:
    rule = _rules()["meiva-ops-health-probe-silent"][1]
    assert "absent_over_time(" in _query(rule)
    assert rule["labels"]["metric"] == "health_probe_silent"


def test_the_warning_and_error_tiers_of_one_outage_share_a_query() -> None:
    for warning, error in (("meiva-ops-health-probe-warning", "meiva-ops-health-probe-error"),):
        assert _query(_rules()[warning][1]) == _query(_rules()[error][1])


def test_grafana_owns_five_minute_recovery_and_missing_series_grace() -> None:
    for group, rule in _rules().values():
        assert rule["keepFiringFor"] == "5m"
        assert rule["missing_series_evals_to_resolve"] == (6 if group == "meiva-ops" else 2)
        assert "keep_firing_for" not in rule


@pytest.mark.parametrize("diagnostic", ["venv", "brew-pin", "browser-reach"])
def test_root_diagnostics_keep_the_machine_identity(diagnostic: str) -> None:
    rule = _rules()["meiva-ops-root-diagnostic-" + diagnostic][1]
    assert "sum by (machine)" in _query(rule)
    assert "{{ $labels.machine }}" in rule["annotations"]["summary"]
