"""Alerts config — AlertsSettings (Task #1224, user design 2026-08-12).

The system→human alert store + notification knobs. Alert is fully separate
from Notice. Grafana owns rule evaluation (rules as code in
deploy/lgtm/config/grafana/provisioning/alerting/rules.yml, delivered through the embedded Alertmanager);
the gateway receives webhook POSTs on /api/alerts, stores them in ``alerts``,
publishes them on the SSE stream, and fans firing/resolved notifications out
to the IM channels the user has connected (services/im_bridge daemon) — every
severity pushes (critical/warning/error, no severity gate). The health probe
and the machine liveness pass write rows directly. This domain is
gateway-owned: only the gateway process reads it. The provider-guard
thresholds (``provider_guard_*``) are read by the cluster health probe's
provider-account checks (``cli/commands/_provider_guard.py``) — same
alert-policy family, same channel.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr

from shared.config._base import EnvSettings


class AlertsSettings(EnvSettings):
    transition_warning_seconds: float = Field(
        default=180.0,
        alias="AVA_ALERTS_TRANSITION_WARNING_SECONDS",
        description=(
            "Seconds an outage transition may remain unexplained before WARNING. "
            "The 2026-08-04 user ruling reserves this window for normal node, "
            "rollout, watchdog self-heal, and network recovery."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    transition_error_seconds: float = Field(
        default=600.0,
        alias="AVA_ALERTS_TRANSITION_ERROR_SECONDS",
        description=(
            "Seconds an unrecovered outage transition may remain open before ERROR. "
            "The 2026-08-04 user ruling grades the preceding interval as WARNING."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

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

    inspect_metrics_degraded_cooldown_seconds: float = Field(
        default=3600.0,
        alias="AVA_ALERTS_INSPECT_METRICS_DEGRADED_COOLDOWN_SECONDS",
        description=(
            "Per-(agent, family, condition) cooldown for the inspector-metrics "
            "coverage warnings the gateway logs, and for re-emission of an open "
            "degradation alert (task #3869). The condition is read-path evaluated: "
            "without a cooldown every statistics request would re-log and re-send "
            "the same chronic limit; an hour keeps the record visible while "
            "holding the volume to one line per condition."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
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

    provider_guard_balance_enabled: bool = Field(
        default=True,
        alias="AVA_PROVIDER_GUARD_BALANCE_ENABLED",
        description=(
            "Run the provider-account balance check in the cluster health probe "
            "(checks 9-10, cli/commands/_provider_guard.py) — the pre-arrears "
            "warning for the 2026-09-18 outage class. Skipped cleanly on hosts "
            "without the provider key configured."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_balance_min_cny: float = Field(
        default=500.0,
        alias="AVA_PROVIDER_GUARD_BALANCE_MIN_CNY",
        description=(
            "Minimum DeepSeek account balance (CNY) before the health probe "
            "fails and alerts. The default approximates one day of the fleet's "
            "observed DeepSeek burn (about $90/day in September 2026 accounting), "
            "so a top-up has roughly a full day of margin to land: the "
            "2026-09-18 balance hit zero at ~05:02 and nobody acted for 5h43m, "
            "while a threshold this far ahead of zero crosses during waking "
            "hours. Ops tunes it to the account's real burn rate."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_balance_url: str = Field(
        default="https://api.deepseek.com/user/balance",
        alias="AVA_PROVIDER_GUARD_BALANCE_URL",
        description=(
            "Balance endpoint the provider-account check reads (GET with the "
            "cluster DEEPSEEK_API_KEY bearer; DeepSeek /user/balance response "
            "shape). Overridable so the low-balance drill can point at a stub "
            "without touching the real account."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_balance_timeout_seconds: float = Field(
        default=20.0,
        alias="AVA_PROVIDER_GUARD_BALANCE_TIMEOUT_SECONDS",
        description=(
            "Per-request timeout for the balance read (seconds). Generous "
            "because the check rides the 300s probe tick, and an unreadable "
            "balance only logs a stderr note and passes — a slow provider API "
            "never fails the probe."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_blocked_agents_enabled: bool = Field(
        default=True,
        alias="AVA_PROVIDER_GUARD_BLOCKED_AGENTS_ENABLED",
        description=(
            "Run the blocked-agents check in the cluster health probe: fails "
            "while several agents are halted by permanent provider rejections "
            "(the recovery breaker's durable halt). The Grafana billing rule "
            "reports the first post-arrears rejection as an event; this check "
            "covers every permanent class (auth / forbidden / model-not-found "
            "included) as a state that keeps alerting while the fleet is still "
            "halted."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_blocked_agents_min: int = Field(
        default=3,
        alias="AVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN",
        description=(
            "How many agents halted by permanent provider rejections trip the "
            "blocked-agents check. Three correlates a fleet-wide cause on a "
            "~20-30 agent fleet: one or two stuck agents stay below it (their "
            "ancestor escalations already fire), while the 2026-09-18 wave "
            "halted 23 agents. Ops tunes it to the fleet size."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    provider_guard_blocked_agents_window_hours: float = Field(
        default=24.0,
        alias="AVA_PROVIDER_GUARD_BLOCKED_AGENTS_WINDOW_HOURS",
        description=(
            "Recency window for the blocked-agents check: only agents whose "
            "last fatal turn falls inside it count. Bounds the count to the "
            "active wave and lets rows nobody revived age out instead of "
            "camping the check red forever — the batch-revive flow owns "
            "leftovers."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
