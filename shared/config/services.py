"""Services config — ServiceSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Browser, helper, and memory-service
fields are inherited from `service_runtime`; aggregated by shared/config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import _unit_home
from shared.config.service_health_ports_fields import ServiceHealthPortFields
from shared.config.service_runtime import _ServiceRuntimeSettings


class ServiceSettings(ServiceHealthPortFields, _ServiceRuntimeSettings):
    agent_host_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "agent-host.pid",
        alias="AVA_AGENT_HOST_PIDFILE",
        description="Hosted agent-runner daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    page_server_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "page-server.pid",
        alias="AVA_PAGE_SERVER_PIDFILE",
        description="Page server supervisor daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "labeler.pid",
        alias="AVA_LABELER_PIDFILE",
        description="Labeler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_max_chars: int = Field(
        default=64,
        gt=0,
        alias="AVA_LABELER_MAX_CHARS",
        description=(
            "Character ceiling for an auto-generated agent label: the model is "
            "asked for a label of at most this many characters, and the output is "
            "truncated to the same number. 64 is a readable single line in the "
            "fleet view — long enough to distinguish one task from another, short "
            "enough that labels scan side by side."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "heartbeat.pid",
        alias="AVA_HEARTBEAT_PIDFILE",
        description="Heartbeat daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "delivery_watchdog.pid",
        alias="AVA_DELIVERY_WATCHDOG_PIDFILE",
        description="Delivery watchdog daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "task_maintenance.pid",
        alias="AVA_TASK_MAINTENANCE_PIDFILE",
        description="Task-maintenance daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "events_maintenance.pid",
        alias="AVA_EVENTS_MAINTENANCE_PIDFILE",
        description="Events-maintenance daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pg_backup.pid",
        alias="AVA_PG_BACKUP_PIDFILE",
        description="Postgres backup scheduler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    backup_hour: int = Field(
        default=3,
        ge=0,
        le=23,
        alias="AVA_BACKUP_HOUR",
        description=(
            "Cluster-clock hour (0-23) at which the daily logical dump becomes due: "
            "the scheduler's first wake at/after this hour starts it, so a host that "
            "was down at that hour catches up on its next tick. 03:00 runs in the "
            "quiet window, and the newest dump is then only hours old when the "
            "morning reads it; the hour is read on AVA_TIMEZONE (cluster time), "
            "never the host clock."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    backup_keep: int = Field(
        default=7,
        gt=0,
        alias="AVA_BACKUP_KEEP",
        description=(
            "How many of the newest daily dumps the local pool keeps; the off-site "
            "PITR retention planner mirrors this count so the remote namespace "
            "cannot drift from the local prune. 7 is a week of dailies: a bad "
            "migration found a day later must not have already overwritten the last "
            "good copy. The pre-update/activation snapshots and the in-flight "
            "activation pin are kept in their own slots on top of this window."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    pitr_uploader_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pitr_uploader.pid",
        alias="AVA_PITR_UPLOADER_PIDFILE",
        description="PITR uploader daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_base_backup_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "pitr_base_backup.pid",
        alias="AVA_PITR_BASE_BACKUP_PIDFILE",
        description="PITR base candidate scheduler daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    gateway_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "gateway.pid",
        alias="AVA_GATEWAY_PIDFILE",
        description="Gateway uvicorn pidfile path. healthcheck goes via HTTP; this is auxiliary only.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    gateway_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "gateway-watchdog.pid",
        alias="AVA_GATEWAY_WATCHDOG_PIDFILE",
        description="Gateway-capability watchdog daemon pidfile path (the monitor itself, no one monitors it).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_runner_watchdog_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "agent-runner-watchdog.pid",
        alias="AVA_AGENT_RUNNER_WATCHDOG_PIDFILE",
        description="Agent-runner-capability watchdog daemon pidfile path (the monitor itself, no one monitors it).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    frontend_healthcheck_url: str = Field(
        default="http://localhost:3000",
        alias="AVA_FRONTEND_HEALTHCHECK_URL",
        description="The fleet UI entry the user reaches — the always-up gate's port (the Next.js app itself binds AVA_APP_PORT and is proxied).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    app_port: int | None = Field(
        default=None,
        alias="AVA_APP_PORT",
        description="The Next.js app port the gate proxies to (the gate owns the entry port). Unset = entry port + 1; converge writes the explicit value from the cluster record.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_health_url: str = Field(
        default="http://localhost:8000/api/health",
        alias="AVA_GATEWAY_HEALTH_URL",
        description="Gateway healthcheck probe URL. Pure agent-runners derive it from AVA_GATEWAY_URL when no host override is set; gateway-capable units default to the local gateway.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_health_url: str = Field(
        default="",
        alias="AVA_LABELER_HEALTH_URL",
        description="Labeler healthcheck URL. Empty = derive via shared.daemon_health.health_port('labeler').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_enabled: bool = Field(
        default=True,
        alias="AVA_IM_BRIDGE_ENABLED",
        description="Whether the im_bridge service is part of this cluster's service roster. A cluster with no IM adapters configured (all Telegram/Weixin/Feishu credentials absent) can disable the service: its daemon exits immediately with zero adapters, and the watchdog's healthcheck then fails it every round (2026-08-10 preview noise).",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_health_url: str = Field(
        default="",
        alias="AVA_IM_BRIDGE_HEALTH_URL",
        description="IM Bridge healthcheck URL. Empty = derive via shared.daemon_health.health_port('im_bridge').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_disabled_adapters: Annotated[list[str], NoDecode] = Field(
        default=[],
        alias="AVA_IM_DISABLED_ADAPTERS",
        description="Comma-separated IM adapter names to skip at daemon load (code stays; e.g. 'weixin,feishu' — user ruling 2026-08-06 keeps only Telegram live).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_send_retry_delays: Annotated[list[float], NoDecode] = Field(
        default=[2.0, 4.0, 8.0, 16.0, 32.0],
        alias="AVA_IM_SEND_RETRY_DELAYS",
        description="Comma-separated backoff delays (seconds) between gateway enqueue retries. A gateway mid-rollout is down for roughly a minute; 2+4+8+16+32 covers it, then the message is dropped (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_sse_read_timeout_seconds: float = Field(
        default=120.0,
        alias="AVA_IM_SSE_READ_TIMEOUT_SECONDS",
        description="Read timeout (seconds) for the IM Bridge SSE subscription stream. The stream is long-lived and mostly idle (the gateway sends a keep-alive comment about once a second), so this only trips on a genuinely dead connection (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_timeline_window: int = Field(
        default=20,
        gt=0,
        alias="AVA_IM_BRIDGE_TIMELINE_WINDOW",
        description=(
            "Raw timeline items the IM bridge fetches for a /switch replay before "
            "filtering to dialog messages: the raw feed mixes in non-dialog items "
            "(agent_updated, task events), so the fetch window must be wider than "
            "the replayed count. 20 leaves room for the filter to still find a full "
            "batch behind non-dialog items (user feedback 2026-08-05: a raw limit "
            "of 5 could yield as few as 2 dialog messages)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_replay_messages: int = Field(
        default=5,
        gt=0,
        alias="AVA_IM_BRIDGE_REPLAY_MESSAGES",
        description=(
            "How many of the most recent dialog messages a /switch replay pushes "
            "to the chat. 5 gives enough scroll-back to re-orient without flooding "
            "the chat window (user feedback 2026-08-05: 'only the last 2 is too "
            "few')."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_notice_reply_window_seconds: int = Field(
        default=300,
        gt=0,
        alias="AVA_IM_BRIDGE_NOTICE_REPLY_WINDOW_SECONDS",
        description=(
            "How long after tapping [Reply] on a notice the chat stays in reply "
            "mode — plain text sent in the window answers the notice. 300 (5 "
            "minutes) covers reading the notice and typing an answer; shorter "
            "risks the mode expiring mid-reply, longer leaves a forgotten mode "
            "swallowing unrelated chat messages (/cancel exits early)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_bridge_notice_open_limit: int = Field(
        default=50,
        gt=0,
        alias="AVA_IM_BRIDGE_NOTICE_OPEN_LIMIT",
        description=(
            "Cap on one open-notices listing for the /notice queue view (the "
            "direct-DB read and the HTTP fallback both pass it). Each listed "
            "notice is pushed as its own chat message, so 50 bounds the "
            "worst-case burst a single queue command can send."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_health_url: str = Field(
        default="",
        alias="AVA_HEARTBEAT_HEALTH_URL",
        description="Heartbeat healthcheck URL. Empty = derive via shared.daemon_health.health_port('heartbeat').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "im_bridge.pid",
        alias="AVA_IM_BRIDGE_PIDFILE",
        description="IM Bridge daemon pidfile path.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_health_url: str = Field(
        default="",
        alias="AVA_TASK_MAINTENANCE_HEALTH_URL",
        description="Task-maintenance healthcheck URL. Empty = derive via shared.daemon_health.health_port('task_maintenance').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_health_url: str = Field(
        default="",
        alias="AVA_EVENTS_MAINTENANCE_HEALTH_URL",
        description="Events-maintenance healthcheck URL. Empty = derive via shared.daemon_health.health_port('events_maintenance').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_health_url: str = Field(
        default="",
        alias="AVA_PG_BACKUP_HEALTH_URL",
        description="Postgres backup scheduler healthcheck URL. Empty = derive via shared.daemon_health.health_port('pg_backup').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    watchdog_interval_seconds: float = Field(
        default=60.0,
        alias="AVA_WATCHDOG_INTERVAL_SECONDS",
        description="Watchdog daemon healthcheck round interval (seconds).",
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    watchdog_respawn_backoff_cap_seconds: float = Field(
        default=1800.0,
        alias="AVA_WATCHDOG_RESPAWN_BACKOFF_CAP_SECONDS",
        description=(
            "Watchdog respawn exponential-backoff ceiling (seconds). After a failed "
            "respawn, the next attempt is delayed by base * 2^n (base = watchdog round "
            "interval), capped at this value; a condition a respawn cannot cure stops "
            "being hammered once the delay exceeds it."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    watchdog_respawn_breaker_rounds: int = Field(
        default=5,
        alias="AVA_WATCHDOG_RESPAWN_BREAKER_ROUNDS",
        description=(
            "Watchdog respawn circuit breaker: consecutive rounds without a probe-alive "
            "verdict that open the breaker — respawns stop and hold, with one "
            "respawn_breaker_open alert per episode, until a round probes alive."
        ),
        json_schema_extra={
            "capability": "common",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    ops_pidfile: Path = Field(
        default_factory=lambda: _unit_home() / "run" / "ops.pid",
        alias="AVA_OPS_PIDFILE",
        description="ava-ops daemon pidfile path.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_concurrency: int = Field(
        default=8,
        alias="AVA_OPS_CONCURRENCY",
        description="Max concurrent cluster ops the agent-runner executes; further inbound /ops requests queue. Prevents one fan-out from overwhelming the runner.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    @field_validator("im_send_retry_delays", mode="before")
    @classmethod
    def _parse_delay_list(cls, v: object) -> object:
        """Env values for delay-list fields arrive as raw strings (NoDecode
        keeps pydantic-settings from JSON-decoding them). Accept both spellings:
        a JSON array ("[1.0, 5.0]") or a comma-separated list ("1.0,5.0")."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [float(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("im_disabled_adapters", mode="before")
    @classmethod
    def _parse_str_list(cls, v: object) -> object:
        """AVA_IM_DISABLED_ADAPTERS arrives as a raw string (NoDecode).
        Accept a JSON array ("[\"weixin\", \"feishu\"]"), a comma-separated
        list ("weixin,feishu"), or an empty value -> [] (Task #855; P0 fix:
        without this validator any env value raised ValidationError and the
        settings load crashed every runner)."""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            try:
                parsed = cast("list[Any]", json.loads(s))
                if isinstance(parsed, list):
                    return [x for x in parsed if isinstance(x, str)]
            except json.JSONDecodeError:
                pass  # fail-fast-ok: not JSON -> fall through to comma split
            return [x.strip() for x in s.split(",") if x.strip()]
        return v
