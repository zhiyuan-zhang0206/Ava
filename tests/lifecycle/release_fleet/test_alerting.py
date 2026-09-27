"""Fleet alert events and their routing: alerts row, out-of-band webhook, observer agent."""

from __future__ import annotations

import json

from cli.release_fleet.alerting import (
    AgentNotice,
    AlertRow,
    FleetAlert,
    Webhook,
    deliveries,
    drain_alerts,
    recovered_alert,
    verdict_alerts,
)
from cli.release_fleet.policy import AlertRoute
from cli.release_fleet.workload import Evidence, UnitReport, judge_start, judge_watch
from shared.alerts import fingerprint, parse_ts
from tests.lifecycle.release_fleet.conftest import (
    GATEWAY,
    OPERATION,
    POLICY,
    RESUMED,
    RUNNER,
    WATCH_END,
    at,
    core_ok,
    healthy,
    two_units,
    units_ready,
)

TEN = two_units(gateway_agents=(1, 2, 3, 4, 5), runner_agents=(6, 7, 8, 9, 10), reaped=(2, 7))
ALL = tuple(range(1, 11))


def _events(alerts: tuple[FleetAlert, ...]) -> list[tuple[str, str | None]]:
    return [(a.kind, None if a.unit is None else a.unit.label) for a in alerts]


def test_a_clean_commit_raises_nothing() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL), direction="candidate", now=WATCH_END
    )
    assert verdict_alerts(OPERATION, POLICY, TEN, verdict) == ()


def test_a_degraded_commit_lists_the_affected_agents() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL[1:]), direction="candidate", now=WATCH_END
    )
    (alert,) = verdict_alerts(OPERATION, POLICY, TEN, verdict)
    assert (alert.kind, alert.agents, alert.severity, alert.at) == (
        "degraded_commit",
        (1,),
        "error",
        WATCH_END,
    )


def test_a_threshold_recovery_raises_unit_threshold_and_recovering() -> None:
    evidence = Evidence(
        since=RESUMED,
        units=(
            *units_ready(at(30), GATEWAY),
            UnitReport(unit=RUNNER, state="failed", observed_at=at(20)),
        ),
        core=core_ok(at(30)),
    )
    verdict = judge_start(POLICY, TEN, evidence, direction="candidate", now=at(40))
    alerts = verdict_alerts(OPERATION, POLICY, TEN, verdict)
    assert _events(alerts) == [
        ("unit_failed", RUNNER.label),
        ("threshold_exceeded", None),
        ("recovering", None),
    ]
    assert alerts[0].agents == (6, 7, 8, 9, 10)
    assert "5 of 10" in alerts[1].summary
    assert "workload threshold exceeded" in alerts[2].summary


def test_a_below_threshold_unit_failure_is_still_reported() -> None:
    cohort = two_units(gateway_agents=tuple(range(1, 10)), runner_agents=(10,))
    evidence = Evidence(
        since=RESUMED,
        units=(
            *units_ready(at(30), GATEWAY),
            UnitReport(unit=RUNNER, state="failed", observed_at=at(20)),
        ),
        core=core_ok(at(30)),
    )
    verdict = judge_start(POLICY, cohort, evidence, direction="candidate", now=at(40))
    assert verdict.action == "proceed"
    assert _events(verdict_alerts(OPERATION, POLICY, cohort, verdict)) == [
        ("unit_failed", RUNNER.label)
    ]


def test_a_hold_names_the_core_reasons() -> None:
    evidence = Evidence(since=RESUMED, units=units_ready(at(30)))
    verdict = judge_start(POLICY, TEN, evidence, direction="previous", now=at(40))
    (held,) = verdict_alerts(OPERATION, POLICY, TEN, verdict)
    assert (held.kind, held.severity) == ("held", "critical")
    assert "shared core database unknown" in held.summary


def test_drain_cancellations_are_one_alert_with_the_agent_list() -> None:
    (alert,) = drain_alerts(OPERATION, TEN)
    assert (alert.kind, alert.agents, alert.at) == ("drain_cancelled", (2, 7), TEN.captured_at)
    assert drain_alerts(OPERATION, two_units(gateway_agents=(1,))) == ()


def test_alert_keys_are_stable_per_operation_event_and_unit() -> None:
    first = recovered_alert(OPERATION, at(5))
    assert first.key == f"{OPERATION}:recovered:*"
    assert recovered_alert(OPERATION, at(9)).key == first.key
    assert FleetAlert(
        operation=OPERATION, kind="unit_failed", unit=GATEWAY, at=at(0), summary="x"
    ).key == (f"{OPERATION}:unit_failed:{GATEWAY.label}")


def test_without_a_route_only_the_alerts_row_is_delivered() -> None:
    (row,) = deliveries(recovered_alert(OPERATION, at(5)), AlertRoute())
    assert isinstance(row, AlertRow)
    payload = row.payload()
    assert payload["status"] == "firing"
    assert payload["labels"] == {
        "alertname": "release fleet: recovered",
        "severity": "error",
        "operation": str(OPERATION),
        "event": "recovered",
    }
    # The alerts ingest's own parser reads back the same instant.
    assert parse_ts(str(payload["starts_at"])) == at(5)
    assert row.source == "release-fleet"


def test_the_alert_row_identity_is_stable_for_a_redelivery() -> None:
    alert = FleetAlert(
        operation=OPERATION, kind="unit_failed", unit=RUNNER, agents=(6,), at=at(0), summary="x"
    )
    (first,) = deliveries(alert, AlertRoute())
    (again,) = deliveries(alert, AlertRoute())
    assert isinstance(first, AlertRow)
    assert isinstance(again, AlertRow)
    assert fingerprint(first.labels) == fingerprint(again.labels)
    assert first.labels["unit"] == RUNNER.label
    assert first.annotations == {"summary": "x", "agents": "6"}


def test_a_full_route_adds_the_webhook_and_the_observer_agent() -> None:
    route = AlertRoute(recipient_agent=1818, webhook_file="release-webhook")
    alert = FleetAlert(
        operation=OPERATION, kind="held", agents=(3, 4), at=at(7), summary="held for the operator"
    )
    row, hook, notice = deliveries(alert, route)
    assert isinstance(row, AlertRow)
    assert isinstance(hook, Webhook)
    assert isinstance(notice, AgentNotice)
    assert hook.url_file == "release-webhook"
    body = json.loads(hook.body.model_dump_json())
    assert body["key"] == alert.key
    assert (body["event"], body["severity"], body["agents"]) == ("held", "critical", [3, 4])
    assert notice.agent == 1818
    assert notice.text == f"[release {OPERATION}] critical held: held for the operator"
