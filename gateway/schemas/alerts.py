"""Schemas for /api/alerts — the system→human alert store (Task #1224).

The ingest side models the Alertmanager standard webhook payload (as Grafana's
embedded Alertmanager delivers it to a webhook contact point): one POST
carries ``status`` + ``alerts[]``, each alert an instance of a rule
(labels/annotations/startsAt/endsAt/fingerprint/values/generatorURL). The
cluster health probe and the heartbeat liveness pass post the same shape
with ``source="health-probe"`` / ``source="machine-probe"``, so every
producer rides one ingest pipeline. The query side is the alert section's
history list (unresolved-first) + mark-as-read; the SSE stream publishes the
same row shape the list returns.

Alert is fully separate from Notice — nothing here touches agent_notices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Label parsing helpers live in shared/alerts.py — the ingest core shared
# with the health probe and the machine liveness pass.
from shared.alerts import parse_alertname, parse_severity

__all__ = [
    "AlertIngestResult",
    "AlertRow",
    "AlertSeverity",
    "AlertStatus",
    "AlertWebhookAlert",
    "AlertWebhookPayload",
    "AlertsListMeta",
    "AlertsListResponse",
    "AlertsReadRequest",
    "parse_alertname",
    "parse_severity",
]

# Store vocabulary (user design 2026-08-12): three severities, two statuses.
AlertSeverity = Literal["critical", "warning", "error"]
AlertStatus = Literal["unresolved", "resolved"]
# The webhook vocabulary Alertmanager sends (mapped to store vocabulary on ingest).
WebhookStatus = Literal["firing", "resolved"]


class AlertWebhookAlert(BaseModel):
    """One alert instance inside an Alertmanager webhook POST.

    ``labels`` / ``annotations`` are free-form; extra per-alert fields
    Alertmanager adds over time (``silenceURL``, ``imageURL``, …) are
    tolerated, not rejected.
    """

    model_config = ConfigDict(extra="ignore")

    status: WebhookStatus = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="", alias="startsAt")
    ends_at: str = Field(default="", alias="endsAt")
    fingerprint: str = ""
    generator_url: str = Field(default="", alias="generatorURL")
    values: dict[str, Any] | None = None


class AlertWebhookPayload(BaseModel):
    """Top level of an Alertmanager webhook POST.

    Tolerates both the full Alertmanager v4 envelope (version/groupKey/
    receiver/commonLabels/… — ignored here) and the slimmer Grafana-managed
    webhook shape; only ``status`` + ``alerts[]`` matter to the store.
    ``source`` tags the row's provenance: the webhook omits it (default
    ``grafana``), while the health probe posts ``source="health-probe"`` and
    the liveness pass ``source="machine-probe"``.
    """

    model_config = ConfigDict(extra="ignore")

    source: str = "grafana"
    status: WebhookStatus | None = None
    alerts: list[AlertWebhookAlert] = Field(default_factory=list)

    def flattened(self) -> list[dict[str, Any]]:
        """Each alert as the plain dict shared/alerts.py consumes, with the
        top-level status as the per-alert fallback (the Grafana-managed
        webhook carries status only at the top level)."""

        out: list[dict[str, Any]] = []
        for alert in self.alerts:
            d = alert.model_dump(mode="json")
            if self.status is not None and not d.get("status"):
                d["status"] = self.status
            out.append(d)
        return out


class AlertIngestResult(BaseModel):
    """What one ingest POST did — counts the UI/ops can log."""

    model_config = ConfigDict(frozen=True)

    processed: int
    inserted: int
    updated: int
    notified: int


class AlertRow(BaseModel):
    """One alerts row as the UI sees it (list + SSE frames)."""

    model_config = ConfigDict(frozen=True)

    id: int
    status: AlertStatus
    severity: AlertSeverity
    alertname: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    fingerprint: str
    generator_url: str
    source: str
    read_at: datetime | None
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AlertsListMeta(BaseModel):
    """Query provenance of one GET /api/alerts response."""

    model_config = ConfigDict(frozen=True)

    window: str
    include_read: bool
    total: int  # rows matching the filters before the limit
    unresolved_count: int  # unresolved rows matching the filters (the floating bar)
    unread_count: int  # unread rows matching the filters (the top-bar badge)


class AlertsListResponse(BaseModel):
    """Unresolved-first alert history + the counts the UI badges show."""

    model_config = ConfigDict(frozen=True)

    alerts: list[AlertRow]
    meta: AlertsListMeta


class AlertsReadRequest(BaseModel):
    """PATCH /api/alerts/read body — mark by ids, or everything."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] | None = None
    all: bool = False

    @model_validator(mode="after")
    def _require_target(self) -> AlertsReadRequest:
        if not self.all and not self.ids:
            raise ValueError("pass ids[] or all=true")
        return self
