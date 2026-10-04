"""Services config — ServiceSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Browser, helper, and memory-service
fields are inherited from `service_runtime`; aggregated by base/config.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from base.config.base import _unit_home
from base.config.domains.services.health_ports_fields import ServiceHealthPortFields
from base.config.domains.services.runtime import _ServiceRuntimeSettings


class ServiceSettings(ServiceHealthPortFields, _ServiceRuntimeSettings):
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
            "How many of the newest daily dumps the local pool keeps. 7 is a week "
            "of dailies: a bad migration found a day later must not have already "
            "overwritten the last good copy."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    backup_offsite_endpoint: str = Field(
        default="",
        alias="AVA_BACKUP_OFFSITE_ENDPOINT",
        description=(
            "Aliyun OSS region endpoint (e.g. https://oss-cn-shanghai.aliyuncs.com) "
            "the daily dump is published to. The off-site leg is skipped, with one "
            "log line, until the endpoint, the bucket and the credentials file are "
            "all set."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    backup_offsite_bucket: str = Field(
        default="",
        alias="AVA_BACKUP_OFFSITE_BUCKET",
        description="Aliyun OSS bucket the daily dump is published to (under `ava-logical/`).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    backup_offsite_credentials_file: Path | None = Field(
        default=None,
        alias="AVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        description=(
            "0600 JSON holding the Aliyun OSS RAM AccessKey pair "
            "(access_key_id, access_key_secret) the off-site publish uses. "
            "Never the cluster secret."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
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

    frontend_healthcheck_url: str = Field(
        default="http://localhost:3000",
        alias="AVA_FRONTEND_HEALTHCHECK_URL",
        description="The fleet UI entry the user reaches — Gate's port (the Next.js app itself binds AVA_APP_PORT and is proxied).",
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
        description="IM Bridge healthcheck URL. Empty = derive from the im_bridge row of base.daemon.endpoints.ServiceEndpoints.",
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
        description="Comma-separated IM adapter names to skip at daemon load (code stays; e.g. 'weixin' — WeChat iLink production-disabled since 2026-08-06; Feishu re-enabled 2026-09).",
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

    im_push_retry_backoff_seconds: float = Field(
        default=1.0,
        ge=0.0,
        alias="AVA_IM_PUSH_RETRY_BACKOFF_SECONDS",
        description=(
            "Base wait (seconds) before an im_bridge outbound send is retried once "
            "(the push path and the ops-alert notify leg share it). Connection-level "
            "failures cluster in a ~0.65s window (probe, task #4252), so an immediate "
            "retry re-enters the same window; 1.0s plus up to 2.0s of jitter "
            "(im_push_retry_jitter_seconds) walks past it."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    im_push_retry_jitter_seconds: float = Field(
        default=2.0,
        ge=0.0,
        alias="AVA_IM_PUSH_RETRY_JITTER_SECONDS",
        description=(
            "Upper bound (seconds) of the uniform jitter added to "
            "im_push_retry_backoff_seconds before an im_bridge outbound retry "
            "(task #4252): spreads concurrent retries so they do not all re-enter "
            "the platform endpoint in lockstep. 0 disables the jitter."
        ),
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
            with suppress(json.JSONDecodeError):  # not JSON -> fall through to comma split
                parsed = cast("list[Any]", json.loads(s))
                if isinstance(parsed, list):
                    return [x for x in parsed if isinstance(x, str)]
            return [x.strip() for x in s.split(",") if x.strip()]
        return v
