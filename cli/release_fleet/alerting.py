"""Fleet release alerts and their routing, as data the coordinator delivers.

Pure: every alert is derived from a journaled verdict, cohort or outcome, and
every delivery is a record. The coordinator journals each alert by
`key` at first emission and re-delivers from the journal, so a retried
delivery carries the same `at` and the alerts table deduplicates it by
`(fingerprint, starts_at)`. An alert row needs the database; the out-of-band
webhook is what still reaches a person when the cluster it reports is down.
The recipient agent only observes; the coordinator stays the recovery authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from cli.release_fleet.policy import AlertRoute, Cohort, FleetPolicy, UnitKey
from cli.release_fleet.workload import CoreFailure, Verdict
from cli.release_transition.request import Record

AlertEvent = Literal[
    "unit_failed",
    "unit_unknown",
    "drain_cancelled",
    "threshold_exceeded",
    "recovering",
    "recovered",
    "held",
    "degraded_commit",
]
Severity = Literal["warning", "error", "critical"]
_SEVERITY: dict[AlertEvent, Severity] = {
    "unit_failed": "error",
    "unit_unknown": "error",
    "drain_cancelled": "warning",
    "threshold_exceeded": "critical",
    "recovering": "critical",
    "recovered": "error",
    "held": "critical",
    "degraded_commit": "error",
}


class FleetAlert(Record):
    operation: UUID
    kind: AlertEvent
    unit: UnitKey | None = None
    agents: tuple[int, ...] = ()
    at: AwareDatetime
    summary: str = Field(min_length=1)

    @property
    def key(self) -> str:
        """Stable identity of one alert within one operation.

        Every hold is its own episode: an operation the operator continued
        that holds again alerts again, so a `held` alert is keyed by its time.
        """
        subject = "*" if self.unit is None else self.unit.label
        if self.kind == "held":
            subject = self.at.isoformat()
        return f"{self.operation}:{self.kind}:{subject}"

    @property
    def severity(self) -> Severity:
        return _SEVERITY[self.kind]


class AlertRow(Record):
    """One `alerts` instance for `shared.alerts.upsert_alert(conn, payload, source)`."""

    kind: Literal["alert_row"] = "alert_row"
    source: Literal["release-fleet"] = "release-fleet"
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: AwareDatetime

    def payload(self) -> dict[str, object]:
        """The Alertmanager-webhook alert shape the alerts ingest reads."""
        return {
            "status": "firing",
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "starts_at": self.starts_at.isoformat(),
            "ends_at": "",
            "generator_url": "",
        }


class WebhookBody(Record):
    version: Literal[1] = 1
    key: str
    operation: UUID
    event: AlertEvent
    severity: Severity
    at: AwareDatetime
    unit: str | None
    agents: tuple[int, ...]
    summary: str


class Webhook(Record):
    """POST `body` as JSON to the URL held in `$AVA_HOME/secrets/<url_file>`."""

    kind: Literal["webhook"] = "webhook"
    url_file: str
    body: WebhookBody


class AgentNotice(Record):
    kind: Literal["agent"] = "agent"
    agent: int = Field(ge=1)
    text: str


Delivery = AlertRow | Webhook | AgentNotice


def deliveries(alert: FleetAlert, route: AlertRoute) -> tuple[Delivery, ...]:
    """Every alert goes to the alerts table, and to each configured route."""
    labels = {
        "alertname": f"release fleet: {alert.kind}",
        "severity": alert.severity,
        "operation": str(alert.operation),
        "event": alert.kind,
    }
    if alert.unit is not None:
        labels["unit"] = alert.unit.label
    annotations = {"summary": alert.summary}
    if alert.agents:
        annotations["agents"] = ",".join(str(agent) for agent in alert.agents)
    routed: list[Delivery] = [AlertRow(labels=labels, annotations=annotations, starts_at=alert.at)]
    if route.webhook_file is not None:
        body = WebhookBody(
            key=alert.key,
            operation=alert.operation,
            event=alert.kind,
            severity=alert.severity,
            at=alert.at,
            unit=None if alert.unit is None else alert.unit.label,
            agents=alert.agents,
            summary=alert.summary,
        )
        routed.append(Webhook(url_file=route.webhook_file, body=body))
    if route.recipient_agent is not None:
        text = f"[release {alert.operation}] {alert.severity} {alert.kind}: {alert.summary}"
        routed.append(AgentNotice(agent=route.recipient_agent, text=text))
    return tuple(routed)


def drain_alerts(operation: UUID, cohort: Cohort) -> tuple[FleetAlert, ...]:
    """`drain_cancelled`: the bounded drain interrupted these agents' turns."""
    reaped = tuple(sorted(agent for entry in cohort.units for agent in entry.reaped))
    if not reaped:
        return ()
    summary = f"the bounded drain cancelled in-flight turns of {len(reaped)} cohort agents"
    return (
        FleetAlert(
            operation=operation,
            kind="drain_cancelled",
            agents=reaped,
            at=cohort.captured_at,
            summary=summary,
        ),
    )


def recovered_alert(operation: UUID, at: datetime) -> FleetAlert:
    """The operation completed on the previous release; the candidate is rejected."""
    return FleetAlert(
        operation=operation,
        kind="recovered",
        at=at,
        summary="recovered to the previous release; the candidate is recorded as rejected",
    )


def verdict_alerts(
    operation: UUID, policy: FleetPolicy, cohort: Cohort, verdict: Verdict
) -> tuple[FleetAlert, ...]:
    """Alerts a journaled verdict raises, reported even below the threshold."""
    alerts = [
        *_unit_alerts(operation, cohort, verdict, "unit_failed", verdict.failed_units),
        *_unit_alerts(operation, cohort, verdict, "unit_unknown", verdict.unknown_units),
    ]
    affected = tuple(entry.agent for entry in verdict.affected)
    if verdict.threshold_exceeded:
        summary = (
            f"{len(affected)} of {verdict.cohort_size} cohort agents affected "
            f"(threshold {policy.threshold_percent}%, minimum {policy.min_affected})"
        )
        alerts.append(_alert(operation, verdict, "threshold_exceeded", summary, affected))
    if verdict.action in {"recover", "hold"}:
        event: AlertEvent = "recovering" if verdict.action == "recover" else "held"
        reasons = _reasons(verdict)
        summary = (
            f"recovering to the previous release: {reasons}"
            if event == "recovering"
            else f"held for the operator; business stays closed: {reasons}"
        )
        alerts.append(_alert(operation, verdict, event, summary, affected))
    elif verdict.outcome == "degraded":
        summary = (
            f"committed degraded: {len(verdict.failed_units) + len(verdict.unknown_units)} "
            f"units failed or unknown, {len(affected)} of {verdict.cohort_size} agents affected"
        )
        alerts.append(_alert(operation, verdict, "degraded_commit", summary, affected))
    return tuple(alerts)


def _alert(
    operation: UUID, verdict: Verdict, event: AlertEvent, summary: str, agents: tuple[int, ...]
) -> FleetAlert:
    return FleetAlert(
        operation=operation, kind=event, agents=agents, at=verdict.decided_at, summary=summary
    )


def _unit_alerts(
    operation: UUID,
    cohort: Cohort,
    verdict: Verdict,
    event: AlertEvent,
    units: tuple[UnitKey, ...],
) -> list[FleetAlert]:
    state = "failed" if event == "unit_failed" else "did not report"
    return [
        FleetAlert(
            operation=operation,
            kind=event,
            unit=unit,
            agents=cohort.agents_on(unit),
            at=verdict.decided_at,
            summary=f"unit {unit.label} {state}; {len(cohort.agents_on(unit))} cohort agents affected",
        )
        for unit in units
    ]


def _reasons(verdict: Verdict) -> str:
    parts = [_core_text(failure) for failure in verdict.core]
    if verdict.threshold_exceeded:
        parts.append("workload threshold exceeded")
    return "; ".join(parts)


def _core_text(failure: CoreFailure) -> str:
    text = f"shared core {failure.signal} {failure.state}"
    return text if failure.detail is None else f"{text} ({failure.detail})"
