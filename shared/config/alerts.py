"""Alerts config — AlertsSettings (Task #1224, user design 2026-08-12).

The system→human alert store + notification knobs. Alert is fully separate
from Notice. Grafana owns rule evaluation (rules as code in
deploy/lgtm/config/grafana/provisioning/alerting/rules.yml, delivered through the embedded Alertmanager);
the gateway receives webhook POSTs on /api/alerts, stores them in ``alerts``,
publishes them on the SSE stream, and fans firing/resolved notifications out
to the IM channels the user has connected (services/im_bridge daemon) — every
severity pushes (critical/warning/error, no severity gate). The health probe
and the machine liveness pass write rows directly. This domain is
gateway-owned: only the gateway process reads it.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr

from shared.config._base import EnvSettings


class AlertsSettings(EnvSettings):
    grafana_admin_password: SecretStr | None = Field(
        default=None,
        alias="GRAFANA_ADMIN_PASSWORD",
        description=(
            "Admin password for the co-located Grafana API. When set, the gateway "
            "periodically reconciles stored Grafana alert instances against "
            "Grafana's active Alertmanager view."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
        },
    )

    webhook_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AVA_ALERTS_WEBHOOK_TOKEN",
            # Legacy name, kept as a fallback so the Grafana host's existing
            # .env + launchctl env keep working across the cutover without an
            # operator step; the header side accepts both header names too
            # (gateway/routers/alerts.py).
            "AVA_OPS_ALERTS_WEBHOOK_TOKEN",
        ),
        serialization_alias="AVA_ALERTS_WEBHOOK_TOKEN",
        description=(
            "Shared secret the Grafana alert webhook contact point sends as "
            "`X-Alerts-Token` (or the legacy `X-Ops-Alerts-Token`) on "
            "POST /api/alerts. Empty = the ingest endpoint trusts loopback "
            "callers only (Grafana is co-located); set it when the gateway is "
            "reachable from another host."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    im_notify_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AVA_ALERTS_IM_NOTIFY_ENABLED",
            "AVA_OPS_ALERTS_IM_NOTIFY_ENABLED",
        ),
        serialization_alias="AVA_ALERTS_IM_NOTIFY_ENABLED",
        description=(
            "Send firing/resolved alert notifications to the user's connected "
            "IM channels (Telegram / WeChat / Feishu via the im_bridge daemon). "
            "Disable to keep the alerts store + UI only."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
