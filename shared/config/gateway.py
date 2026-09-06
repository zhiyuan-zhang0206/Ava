"""Gateway config — GatewaySettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings


class GatewaySettings(EnvSettings):
    gateway_client_max_retries: int = Field(
        default=3,
        alias="AVA_GATEWAY_MAX_RETRIES",
        description="SDK -> Gateway transient-failure retry count (transport errors + HTTP 429/5xx, idempotent requests only). Gateway cold start is ~0.6s; 3 retries are sufficient.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_client_retry_delay_seconds: float = Field(
        default=1.0,
        alias="AVA_GATEWAY_RETRY_DELAY_SECONDS",
        description="SDK -> Gateway HTTP retry base interval (seconds); each retry doubles it (bounded exponential backoff, 8s cap) plus a per-agent jitter offset.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_client_http_timeout_seconds: float = Field(
        default=20.0,
        alias="AVA_GATEWAY_HTTP_TIMEOUT_SECONDS",
        description="SDK -> Gateway HTTP connect/read timeout (seconds). Wide margin on purpose: spawn is non-idempotent, so a read timeout on a spawn that DID succeed would let the transport retry re-POST it and orphan a second agent.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    sse_disconnect_poll_seconds: float = Field(
        default=1.0,
        alias="AVA_SSE_DISCONNECT_POLL_SECONDS",
        description="Gateway SSE client-disconnect detection poll interval (seconds).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    sse_throttle_rate: float = Field(
        default=10.0,
        alias="AVA_SSE_THROTTLE_RATE",
        description=(
            "Gateway SSE broadcast max pushes per second; each push batches "
            "available Redis events into one frame. Higher = smoother streaming, "
            "more frequent flushes."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    timeline_compact_history: int = Field(
        default=1,
        ge=-1,
        alias="AVA_TIMELINE_COMPACT_HISTORY",
        description=(
            "Number of compact-history segments the timeline may load backward: "
            "0 disables compact history, -1 allows all retained segments, and N "
            "allows the newest N segments."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "per_agent": False,
        },
    )

    cluster_rpc_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_CLUSTER_RPC_TIMEOUT_SECONDS",
        description="Default deadline (seconds) for one gateway -> agent-runner cluster op (ops/cluster_rpc.py dispatch_to_machine). Spawn typically finishes in <5s; lifecycle / config / inventory ops are quick. status_probe passes its own shorter timeout (task #698 G8).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    status_probe_timeout_seconds: float = Field(
        default=8.0,
        alias="AVA_STATUS_PROBE_TIMEOUT_SECONDS",
        description=(
            "Deadline (seconds) for one gateway -> agent-runner status_probe op: "
            "the roster panel and the heartbeat liveness pass share this budget "
            "(raised from the historical 3.0s hardcode, task #1200). A "
            "slow-but-healthy WSL runner's status_snapshot measured 3.07-3.27s "
            "on 2026-08-12, so a 3.0s budget flipped it offline (2 consecutive "
            "probe timeouts -> machine_probe offline) while every service on it "
            "answered /healthz in ~15ms. A genuinely offline host still refuses "
            "fast (connect refused / blackhole), so the wider budget only costs "
            "the anti-jitter margin, never the detection latency of a real outage."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    cluster_rpc_max_retries: int = Field(
        default=3,
        alias="AVA_CLUSTER_RPC_MAX_RETRIES",
        description="Extra attempts (after the first) for a gateway -> agent-runner cluster op on transient infrastructure failure (transport error / 5xx), with bounded exponential backoff + jitter (task #961). Non-idempotent ops (spawn / cluster_update / lifecycle) retry under an auto-generated idempotency key, so a lost response replays the first run instead of duplicating it. 0 = fail fast, the pre-#961 single-shot behaviour. The roster's status_probe passes retries=1 inside its separate total deadline; persistent failures then use per-machine backoff.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_quiesce_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS",
        description=(
            "Maximum wait for normal agent restart, checkpoint flush and execution "
            "exit during update/rollback verification. Default 300 seconds; timeout "
            "aborts without force-killing or migrating. Must be finite and positive."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    schedule_stall_timeout_seconds: float = Field(
        default=1200.0,
        alias="AVA_SCHEDULE_STALL_TIMEOUT_SECONDS",
        description="How long a schedule runner's main thread may sit in one "
        "frame before the stall guard records last_error and hard-exits (the "
        "ScheduleManager then relaunches with backoff). Guards against a hung "
        "gateway / DB call silently eating a schedule's fire window.",
        json_schema_extra={
            "restart_required": "schedule",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    schedule_stall_check_interval_seconds: float = Field(
        default=30.0,
        alias="AVA_SCHEDULE_STALL_CHECK_INTERVAL_SECONDS",
        description="How often the schedule runner's stall guard samples the main thread's frame.",
        json_schema_extra={
            "restart_required": "schedule",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    work_failed_retry_grace_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_WORK_FAILED_RETRY_GRACE_SECONDS",
        description="Minimum age in seconds before the gateway retries an unfinished work-failure delivery. The grace keeps the startup/periodic reconciler from racing the request that registered the event.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_reload: bool = Field(
        default=False,
        alias="AVA_GATEWAY_RELOAD",
        description="Dev-time uvicorn hot-reload switch. Must be false in prod: reload detaches the worker from its session (PPID=1) and zombies hold the port.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_port: int = Field(
        default=8000,
        alias="AVA_GATEWAY_PORT",
        description="uvicorn bind port for the gateway. Override where 8000 is taken; must match the port in AVA_GATEWAY_URL / AVA_GATEWAY_HEALTH_URL.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    gateway_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AVA_GATEWAY_URL",
            # Deprecated alias, scheduled for removal 2026-09-01 (the original
            # 2026-07-01 deadline lapsed; converge now renames the key in every
            # unit's .env, see migrate_primary_gateway_url_key). Until then it
            # still resolves; warn_deprecated_env_aliases() logs a startup nudge
            # when it is the active source so operators rename before the drop-day.
            "AVA_PRIMARY_GATEWAY_URL",
        ),
        serialization_alias="AVA_GATEWAY_URL",
        description="Gateway base URL on the cluster's private network. Set on "
        "every unit: an agent-runner reaches the gateway here, and on the gateway "
        "it is this host's own URL.",
        json_schema_extra={
            "restart_required": "ops",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="AVA_GATEWAY_CORS_ALLOWED_ORIGINS",
        description=(
            "Comma-separated exact browser origins allowed to call the gateway. "
            "Empty derives the frontend origins from gateway and service settings."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    session_cookie_secure: bool | None = Field(
        default=None,
        alias="AVA_GATEWAY_SESSION_COOKIE_SECURE",
        description=(
            "Whether gateway session cookies carry Secure. Unset derives the "
            "policy from AVA_GATEWAY_URL."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    session_ttl_seconds: int = Field(
        default=24 * 3600,
        gt=0,
        alias="AVA_GATEWAY_SESSION_TTL_SECONDS",
        description="Lifetime in seconds for opaque, server-side browser sessions.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    auth_middleware_enabled: bool = Field(
        default=True,
        alias="AVA_AUTH_MIDDLEWARE_ENABLED",
        validation_alias=AliasChoices("AVA_AUTH_MIDDLEWARE_ENABLED", "AVA_SKIP_AUTH"),
        description=(
            "Enable the gateway's HTTP API auth middleware. Set false for e2e "
            "tests (every request passes without auth while the cluster keeps its "
            "secret). An EMPTY AVA_CLUSTER_SECRET also serves the API without "
            "auth — that is the single-box no-secret posture, distinct from this "
            "test knob. The legacy AVA_SKIP_AUTH alias has INVERTED semantics "
            "(AVA_SKIP_AUTH=true means this is false); dotenv_boot translates it "
            "at load and converge renames it."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    grafana_proxy_enabled: bool = Field(
        default=False,
        alias="AVA_GRAFANA_PROXY_ENABLED",
        description=(
            "Reverse-proxy /grafana/* on the gateway to a co-located Grafana "
            "instance (see grafana_host / grafana_port). Off by default so a "
            "cluster without Grafana is unaffected; the browser then 404s on "
            "/grafana/*. When on, the proxy is auth-gated by the same session "
            "cookie / bearer middleware as every other API route and streams "
            "the upstream response chunk-by-chunk."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    grafana_host: str = Field(
        default="127.0.0.1",
        alias="AVA_GRAFANA_HOST",
        description=(
            "Host of the co-located Grafana instance the gateway reverse-proxies "
            "to when grafana_proxy_enabled is on. Loopback by default — Grafana "
            "binds the gateway host itself and the browser never dials it "
            "directly."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    mcp_endpoint_enabled: bool = Field(
        default=False,
        alias="AVA_MCP_ENDPOINT_ENABLED",
        description=(
            "Serve the cluster control plane as an MCP server over Streamable "
            "HTTP at /mcp on the gateway (design task #1212, step 1). Off by "
            "default — an additive surface; the existing mcp-daemon path and "
            "`ava mcp serve` (stdio) are unaffected either way. The endpoint is "
            "auth-gated by the same cluster middleware as every API route."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    grafana_port: int = Field(
        default=3001,
        alias="AVA_GRAFANA_PORT",
        description=(
            "Port of the co-located Grafana instance the gateway reverse-proxies "
            "to when grafana_proxy_enabled is on. Grafana's default HTTP port is "
            "3000; Ava reserves 3001 so the proxy default matches a Grafana "
            "configured to sit outside the frontend's port."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_cors_allowed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value
