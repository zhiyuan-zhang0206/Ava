"""Gateway config — GatewaySettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field

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

    launch_confirm_timeout_seconds: float = Field(
        default=45.0,
        alias="AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS",
        description="Timeout (seconds) for polling a launched agent's pid claim. It must cover the child's whole pre-claim segment — python startup, imports, the schema assert, the placement SELECT — which on a loaded box has run past the old 10s and cost a live child its row. Not the only defense: if the launched process is still alive at the deadline the wait extends once, up to boot_reap_grace_seconds. The prompt is delivered pre-launch, so a timeout here never drops it; the restarter still reaps a genuinely stuck row.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    agent_boot_stall_seconds: float = Field(
        default=30.0,
        alias="AVA_AGENT_BOOT_STALL_SECONDS",
        description="How long an agent's boot may make no progress before the child kills itself. Passed to every child on argv; its own watchdog thread fires when no new boot phase is reached within this window, so it bounds ONE phase rather than the whole boot — a number that does not have to grow with import bloat or box load, unlike launch_confirm_timeout_seconds. Must stay below launch_confirm_timeout_seconds so a stalled child is already dead when the launcher first looks, making the launcher's liveness probe decisive instead of a guess.",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    agent_boot_budget_seconds: float = Field(
        default=90.0,
        alias="AVA_AGENT_BOOT_BUDGET_SECONDS",
        description="Hard ceiling on an agent's whole pre-claim boot, enforced by the child's own watchdog alongside agent_boot_stall_seconds — whichever comes first. Must stay below boot_reap_grace_seconds (pinned by tests/shared/test_timing_topology.py). The stall window alone bounds a boot only at phases x stall, a product that grows silently the moment a boot phase is added; a boot that outlived the grace would have its row taken by the restarter's dead-birth reaper while the child was still alive and progressing — the 2026-07-30 incident's exact failure, relocated from the launcher to the reaper. This ceiling makes 'the child is gone before the reaper can claim' true by construction instead of by arithmetic over the phase count.",
        json_schema_extra={
            "restart_required": "gateway",
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
        default=25.0,
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
        description="Extra attempts (after the first) for a gateway -> agent-runner cluster op on transient infrastructure failure (transport error / 5xx), with bounded exponential backoff + jitter (task #961). Non-idempotent ops (spawn / cluster_update / lifecycle) retry under an auto-generated idempotency key, so a lost response replays the first run instead of duplicating it. 0 = fail fast, the pre-#961 single-shot behaviour. status_probe passes retries=0 explicitly (the roster carries its own per-machine backoff).",
        json_schema_extra={
            "restart_required": "gateway",
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

    auth_middleware_enabled: bool = Field(
        default=True,
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
