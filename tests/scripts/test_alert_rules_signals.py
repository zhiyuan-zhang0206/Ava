"""The Grafana rules that replaced the in-code alert paths, gates and grace windows.

Every rule here reads one declared event off the Loki stream; the table pins its event, its
`for:` (the debounce the old code carried as "N consecutive rounds", a grace window or a
transition clock), its severity and its threshold, and the checks below hold the contract that
cannot drift silently: the event is declared, the labels a rule groups by are fields the event
carries, and the notification text only names labels the query keeps.
"""

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

# Labels every event line carries as stream labels / envelope fields, beside its payload.
_ENVELOPE = frozenset({"agent_id", "machine", "cluster", "category", "level", "process", "source"})


class Signal(NamedTuple):
    event: str
    group: str
    pending: str
    severity: str
    threshold: float = 0


SIGNALS: dict[str, Signal] = {
    # health probe: warning at 3 minutes unhealthy, error at 10 (was transition_severity 180s / 600s)
    "ava-ops-health-probe-warning": Signal("health_probe_failing", "ava-ops", "3m", "warning"),
    "ava-ops-health-probe-error": Signal("health_probe_failing", "ava-ops", "10m", "error"),
    "ava-ops-health-probe-silent": Signal("health_probe_ran", "ava-ops", "0m", "error"),
    "ava-ops-service-start-unready": Signal("service_start_unready", "ava-ops", "0m", "warning"),
    "ava-ops-schedule-verify-failed": Signal("schedule_verify_failed", "ava-ops", "0m", "error"),
    "ava-ops-schedule-stalled": Signal("schedule_stalled", "ava-ops-slow", "0m", "warning"),
    "ava-ops-agent-continuation-lost": Signal("agent_continuation_lost", "ava-ops", "0m", "error"),
    # machine liveness (was the 180s / 600s transition clock on machine_probe)
    "ava-ops-machine-offline-warning": Signal("machine_probe_failed", "ava-ops", "3m", "warning"),
    "ava-ops-machine-offline-error": Signal("machine_probe_failed", "ava-ops", "10m", "error"),
    # application producers that posted to /api/alerts or wrote the table themselves
    "ava-ops-exec-child-boot-failed": Signal("exec_child_boot_failed", "ava-ops", "0s", "warning"),
    "ava-ops-inspect-metrics-gap": Signal(
        "inspect_metrics_coverage_gap", "ava-ops", "0s", "warning"
    ),
    "ava-ops-impersonation-seal-stuck": Signal(
        "impersonation_event_log_incomplete", "ava-ops", "0s", "warning"
    ),
    "ava-ops-impersonation-capture-failed": Signal(
        "impersonation_event_log_incomplete", "ava-ops", "0s", "warning"
    ),
    "ava-ops-im-push-failed": Signal("im_push_failed", "ava-ops", "0s", "warning"),
    "ava-ops-im-feishu-owner-seed-failed": Signal(
        "im_feishu_owner_seed_failed", "ava-ops", "0s", "warning"
    ),
    "ava-ops-hosted-boot-recovery-deferred": Signal(
        "hosted_boot_recovery_deferred", "ava-ops-slow", "0s", "warning", 2
    ),
    # the #4217 gates (delivery grace, per-host cooldown, consecutive-round escalation)
    "ava-ops-delivery-stalled": Signal("delivery_stalled", "ava-ops", "1m", "warning"),
    "ava-ops-db-pool-acquire-slow": Signal("db_pool_acquire_slow", "ava-ops", "2m", "warning"),
    "ava-ops-root-unit-restart-failed": Signal("root_unit_failure_state", "ava-ops", "0m", "error"),
    "ava-ops-root-unit-breaker-open": Signal(
        "root_unit_failure_state", "ava-ops", "0m", "critical"
    ),
    "ava-ops-root-unit-custody-held": Signal("root_unit_failure_state", "ava-ops", "0m", "warning"),
    "ava-ops-root-unit-not-revivable": Signal(
        "root_unit_not_revivable", "ava-ops", "0m", "error", 1
    ),
    "ava-ops-root-diagnostic-venv": Signal("root_diagnostic", "ava-ops", "0m", "warning", 1),
    "ava-ops-root-diagnostic-brew-pin": Signal("root_diagnostic", "ava-ops", "0m", "warning", 1),
    "ava-ops-root-diagnostic-browser-reach": Signal(
        "root_diagnostic", "ava-ops-slow", "0m", "warning", 2
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
    # A condition that goes quiet must read as healthy, not as an incident or a broken rule.
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


@pytest.mark.parametrize("uid", sorted(SIGNALS))
def test_the_rule_reads_its_declared_telemetry_event_in_the_prod_cluster(uid: str) -> None:
    query = _query(_rules()[uid][1])
    assert f'event_name="{SIGNALS[uid].event}"' in query
    spec = EVENTS[SIGNALS[uid].event]
    assert spec.category == "telemetry"
    assert '| json | cluster=".ava"' in query
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
    """The window silence spares `attributes_check="disk_usage"` and Telegram routes
    `attributes_check="gateway_liveness"`: both depend on the rules keeping that label."""
    for uid in ("ava-ops-health-probe-warning", "ava-ops-health-probe-error"):
        assert "sum by (attributes_check)" in _query(_rules()[uid][1])


def test_the_probe_dead_man_fires_on_an_absent_heartbeat_and_routes_to_telegram() -> None:
    rule = _rules()["ava-ops-health-probe-silent"][1]
    assert "absent_over_time(" in _query(rule)
    assert rule["labels"]["metric"] == "health_probe_silent"  # the policy's Telegram route key


def test_the_warning_and_error_tiers_of_one_outage_share_a_query() -> None:
    """Two coexisting rules replace the old in-place escalation (inhibition has no file form)."""
    for warning, error in (("ava-ops-health-probe-warning", "ava-ops-health-probe-error"),):
        assert _query(_rules()[warning][1]) == _query(_rules()[error][1])
