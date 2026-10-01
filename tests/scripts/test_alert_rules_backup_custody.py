"""Alert rules for backup operation custody (quarantined and blocked kinds)."""

from __future__ import annotations

import pytest

from tests.scripts.test_alert_rules import _exprs, _load_rules, _threshold_params


@pytest.mark.parametrize(
    ("uid", "custody", "severity"),
    [
        ("ava-ops-backup-operation-blocked", "blocked", "error"),
        ("ava-ops-backup-operation-quarantined", "quarantined", "warning"),
    ],
)
def test_backup_operation_custody_rules_fire_per_operation_kind(
    uid: str, custody: str, severity: str
) -> None:
    rule = {r["uid"]: r for r in _load_rules()}[uid]
    assert _exprs(rule, "loki") == [
        'sum by (attributes_operation) (count_over_time({service_name="unknown_service", '
        'event_name="backup_operation_custody"} | json | cluster=".ava" | '
        f'category="telemetry" | level="error" | attributes_custody="{custody}" [1h]))'
    ]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert _threshold_params(rule) == [[0]]
    assert rule["labels"]["severity"] == severity
    assert "attributes_operation" in rule["annotations"]["summary"]


@pytest.mark.parametrize(
    ("uid", "verb"),
    [
        ("ava-ops-backup-operation-blocked", "ava backup operations retire"),
        ("ava-ops-backup-operation-quarantined", "ava backup operations status"),
    ],
)
def test_backup_operation_custody_rules_name_the_live_operator_verb(uid: str, verb: str) -> None:
    rule = {r["uid"]: r for r in _load_rules()}[uid]

    assert verb in rule["annotations"]["description"]
    assert "ava pitr operations" not in rule["annotations"]["description"]
