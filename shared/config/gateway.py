"""Gateway config — GatewaySettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, HttpUrl, JsonValue, field_validator
from pydantic_settings import NoDecode

from shared.config._base import EnvSettings
from shared.config.managed_writer_fields import ManagedWriterFields
from shared.config.update_spawn_fields import UpdateSpawnFields

_SCHEDULE_RESTART_METADATA: dict[str, JsonValue] = {
    "restart_required": "schedule",
    "writable": True,
    "sensitive": False,
    "scope": "cluster-pinned",
}


class GatewaySettings(UpdateSpawnFields, ManagedWriterFields, EnvSettings):
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

    status_probe_fastfail_timeout_seconds: float = Field(
        default=5.0,
        alias="AVA_STATUS_PROBE_FASTFAIL_TIMEOUT_SECONDS",
        description=(
            "Fast-fail deadline (seconds) for the roster's status_probe of a "
            "machine that already carries consecutive reachability failures "
            "(task #3507). A known-failed host's re-dial exists only to notice "
            "recovery; allowed the full status_probe_timeout_seconds it would "
            "hang there and drag the whole-table read past the CLI/UI read "
            "budget - the 2026-09-15 mba transition had `ava cluster status` "
            "timing out every time a re-dial hung. 5s: >1.5x the slowest "
            "healthy status_snapshot measured (3.07-3.27s, task #1200), so a "
            "recovered machine still clears its failures on the first "
            "re-probe, while staying well under the 8s read budget. First "
            "contact with a not-yet-failed machine keeps the full budget - "
            "the anti-false-offline margin."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    status_probe_backoff_base_seconds: float = Field(
        default=5.0,
        alias="AVA_STATUS_PROBE_BACKOFF_BASE_SECONDS",
        description=(
            "Base (seconds) of the per-machine status_probe re-probe backoff "
            "(task #3507 lifted the min(5*2**n, 300) schedule literals into "
            "config under the numeric-limits convention). The window after n "
            "consecutive failures is base * 2**n seconds - the first re-dial "
            "waits 10s at this default - capped at "
            "status_probe_backoff_cap_seconds. 5s ties the base to the panel "
            "poll cadence, so an unreachable host is not dialed more than once "
            "per poll interval at the low end."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    status_probe_backoff_cap_seconds: float = Field(
        default=300.0,
        alias="AVA_STATUS_PROBE_BACKOFF_CAP_SECONDS",
        description=(
            "Ceiling (seconds) for the per-machine status_probe re-probe "
            "backoff (task #3507 lifted the min(5*2**n, 300) schedule literals "
            "into config under the numeric-limits convention): a persistently "
            "down host is still re-probed at least this often, so recovery is "
            "noticed within five minutes while steady-state dialing stays "
            "sparse."
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

    update_straggler_reap_seconds: float = Field(
        default=15.0,
        ge=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_STRAGGLER_REAP_SECONDS",
        description=(
            "Grace window for a native cohort agent to reach its turn boundary "
            "during an update/rollback drain (task #4016), counted from its own "
            "restart command's issuance. An agent still un-landed at expiry is "
            "reaped: CAS-marked 'restarting', its in-flight turn interrupted by "
            "the durable reap signal (in-flight exec/LLM work truncated), and "
            "released with the honest drain outcome 'reaped' -- no flush/apply "
            "receipt is fabricated and its claimed ordinary work is re-delivered "
            "when the successor boot or local resume settles the mark. Only "
            "update-family drains reap; interactive pause/stop/restart drains "
            "keep their never-kill contract. 0 disables the reap (the drain "
            "times out and the wave aborts/retries as before). Decoupled from "
            "exec_timeout_seconds; must be finite and non-negative."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_backup_precheck: bool = Field(
        default=True,
        alias="AVA_UPDATE_BACKUP_PRECHECK",
        description=(
            "Refuse `ava cluster update`'s gateway stop while a logical-backup job "
            "or off-site publish is in flight on this host (task #3661; the "
            "2026-09-16 wave abort was the stop meeting the daily dump's off-site "
            "publish). false (0) dispatches anyway."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_managed_writer: bool = Field(
        default=False,
        alias="AVA_UPDATE_MANAGED_WRITER",
        description=(
            "Gate the managed-writer activation flow in `ava cluster update` "
            "(task #4121). false (0): every rollout runs the legacy flow unchanged. "
            "true: a rollout enters managed-writer mode only when both readiness "
            "guards exist and are True -- the checked normal-release activation "
            "(CHECKED_ACTIVATION_READY, task #4117) and the completed rollout wiring "
            "(MANAGED_WRITER_WIRING_COMPLETE, task #4128 E2); with either guard missing "
            "it runs the legacy flow and records a visible blocked decision (rollout log, "
            "rollout telemetry, `managed_writer_blocked` event, `ava cluster "
            "status`). Flip only in the same ceremony as the checked-activation "
            "change (tasks #4117/#4121); roll back by setting false again -- the "
            "next rollout runs legacy."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    pause_lifecycle_wait_seconds: float = Field(
        default=300.0,
        ge=0,
        allow_inf_nan=False,
        alias="AVA_PAUSE_LIFECYCLE_WAIT_SECONDS",
        description=(
            "Bounded wait when preparation meets in-flight work it did not author "
            "(task #3591): an agent lifecycle command (restart / terminate), or "
            "claimed ordinary work on a parked agent, is retried under the same "
            "row locks until it resolves; preparation aborts only after the bound "
            "is spent. The 300s default shares the drain/Phase-A envelope; an early "
            "resolution exits the wait immediately, so the bound is only the "
            "give-up point. Maintenance-authored commands never wait. 0 refuses "
            "immediately (pre-#3591 behavior). Cluster-pinned; must be finite and "
            "non-negative."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    stranded_hold_recovery: bool = Field(
        default=True,
        alias="AVA_STRANDED_HOLD_RECOVERY",
        description=(
            "Bounded automatic completion of a stranded update hold (task #3142). "
            "When an update leg's maintenance hold outlives its owner past the "
            "stranded-hold bound, the pause controller may spend ONE bounded attempt "
            "at the same stop/start/resume sequence an operator would run by hand "
            "(one attempt per episode, 900s cooldown, post-stop phases only; the "
            "gateway capability's watchdog round never initiates a completion — a "
            "unit that also serves agent-runner completes it in that round). False "
            "disables the mechanism "
            "outright — the task #3132 alarm and the manual recipe in "
            "conventions/graceful-maintenance.md remain the recovery path. Read by "
            "the watchdogs on every tick; a change takes effect at their next restart."
        ),
        json_schema_extra={
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    stop_incomplete_recovery: bool = Field(
        default=True,
        alias="AVA_STOP_INCOMPLETE_RECOVERY",
        description=(
            "Caller-side bounded recovery of a half-done update stop (task #3942): an "
            "agent-runner leg whose graceful stop exits non-zero, while its own "
            "maintenance generation still sits between stopping and stopped, spends "
            "ONE bounded attempt at its own internal start before returning the stop "
            "rc. It declines unless the episode is provably its own. False = the "
            "pre-#3942 behaviour: only the stranded-hold completion, the watchdog's "
            "respawn, or a manual `ava start` restores the host."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    stop_incomplete_recovery_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_STOP_INCOMPLETE_RECOVERY_TIMEOUT_SECONDS",
        description=(
            "Deadline of that one bounded attempt (task #3942): the internal `ava "
            "start` child is killed at this bound, so a hung start cannot hold the "
            "updater's verdict past a bounded window. The 120s default covers a "
            "normal start on a healthy host (launch plus readiness); a start still "
            "running at the bound defers to the paths above. Must be finite and "
            "positive."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hold_watchdog_min_age_seconds: float = Field(
        default=1800.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_HOLD_WATCHDOG_MIN_AGE_SECONDS",
        description=(
            "The minimum age of an orphaned maintenance hold before the OS-scheduled "
            "hold watchdog may complete it (task #3887). An ownerless hold past this "
            "bound is completed once through the official stop/start/resume ladder; "
            "below it the watchdog only observes. 30 minutes is the same window the "
            "pre-stop automatic release uses (task #3270): a transition still in "
            "flight - or an operator about to return - is never misread. Must be "
            "finite and positive. Read by the watchdog job on every run."
        ),
        json_schema_extra={
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hold_watchdog_cooldown_seconds: float = Field(
        default=900.0,
        ge=0,
        allow_inf_nan=False,
        alias="AVA_HOLD_WATCHDOG_COOLDOWN_SECONDS",
        description=(
            "The cooldown between two hold-watchdog attempts within one hold "
            "generation (task #3887). The mechanism spends one attempt per episode "
            "(task #3142's budget shape); the cooldown additionally forbids a second "
            "attempt - a re-declared record, or a state still settling from the "
            "first attempt - from being spent immediately. Must be finite and "
            "non-negative. Read by the watchdog job on every run."
        ),
        json_schema_extra={
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    abandoned_hold_auto_release: bool = Field(
        default=True,
        alias="AVA_ABANDONED_HOLD_AUTO_RELEASE",
        description=(
            "Bounded automatic release of an abandoned pre-stop maintenance hold "
            "(task #3270). When a pre-stop hold past the notice bound has no "
            "shepherding process left and nothing is executing under it, and no "
            "continuation failure blocks `resume --cancel`, the pause controller "
            "may perform that release itself once the hold has survived a 30-minute "
            "observation window; the declaration and the release are both loud "
            "(ops log + stranded-hold record). False disables the automatic release "
            "-- the loud declaration stays; the release is manual. Read by the "
            "watchdogs on every tick; a change takes effect at their next restart."
        ),
        json_schema_extra={
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    schedule_stall_timeout_seconds: float = Field(
        default=1200.0,
        alias="AVA_SCHEDULE_STALL_TIMEOUT_SECONDS",
        description="How long a schedule runner may stall in one frame before child cleanup, bounded failure recording and hard exit. Guards against hung gateway / DB calls; ScheduleManager relaunches with backoff.",
        json_schema_extra=_SCHEDULE_RESTART_METADATA,
    )
    schedule_stall_check_interval_seconds: float = Field(
        default=30.0,
        alias="AVA_SCHEDULE_STALL_CHECK_INTERVAL_SECONDS",
        description="How often the schedule runner's stall guard samples the main thread's frame.",
        json_schema_extra=_SCHEDULE_RESTART_METADATA,
    )

    schedule_stall_exit_record_deadline_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_SCHEDULE_STALL_EXIT_RECORD_DEADLINE_SECONDS",
        description="Total seconds allowed for both failure-record writes after stall child cleanup. The 10-second default allows ordinary DB latency while bounding exit when the DB is wedged; tune for cluster latency.",
        json_schema_extra=_SCHEDULE_RESTART_METADATA,
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

    gateway_graceful_shutdown_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_GATEWAY_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
        description=(
            "Seconds uvicorn may drain in-flight connections after SIGTERM before it "
            "cancels the remaining request/stream tasks and enters lifespan shutdown. "
            "Bounds the connection-drain phase that stalled production on 2026-09-17: "
            "with uvicorn's default (None) an unfinished SSE response held the drain in "
            "'Waiting for connections to close' until a forced kill. Derivation: the "
            "default maintenance stop deadline is 300s, and both this drain and the "
            "lifespan cleanup that follows it must fit inside that deadline, so the "
            "budget sits near a tenth of it; it also exceeds ordinary slow-request "
            "durations, so a planned restart still lets normal work finish. A stuck "
            "stream costs at most this budget, never the stop."
        ),
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
        alias="AVA_GATEWAY_URL",
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

    browser_origin: str = Field(
        default="",
        alias="AVA_BROWSER_ORIGIN",
        description=(
            "Optional HTTPS origin serving the frontend and gateway through one reverse proxy. "
            "Only browsers visiting this exact origin use same-origin API/SSE; existing direct "
            "frontend URLs retain their gateway-port routing. Requires a frontend rebuild."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @field_validator("browser_origin")
    @classmethod
    def _browser_origin(cls, value: str) -> str:
        if not value:
            return ""
        parsed = HttpUrl(value)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in (None, "/")
            or parsed.query is not None
            or parsed.fragment is not None
        ):
            raise ValueError(
                "AVA_BROWSER_ORIGIN must be an HTTPS origin without credentials or path"
            )
        return str(parsed).removesuffix("/")

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
        description=(
            "Enable the gateway's HTTP API auth middleware. Set false for e2e "
            "tests (every request passes without auth while the cluster keeps its "
            "secret). An EMPTY AVA_CLUSTER_SECRET also serves the API without "
            "auth — that is the single-box no-secret posture, distinct from this "
            "test knob."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    login_max_failures: int = Field(
        default=5,
        gt=0,
        alias="AVA_GATEWAY_LOGIN_MAX_FAILURES",
        description=(
            "Consecutive failed logins from one IP before the login endpoint locks "
            "that IP out (429 + Retry-After; while locked the credential is not "
            "evaluated at all). Five is the standard admin-surface lockout threshold: "
            "a handful of typos never locks anyone out, while a scripted guesser hits "
            "the wall on its fifth attempt. The failure state is in-memory in the "
            "single gateway process, so a restart re-arms the counter."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    login_lockout_seconds: int = Field(
        default=900,
        gt=0,
        alias="AVA_GATEWAY_LOGIN_LOCKOUT_SECONDS",
        description=(
            "How long (seconds) an IP stays locked once it trips login_max_failures "
            "consecutive failures. 900 (15 minutes) makes a sustained single-IP "
            "attack impractically slow and forces IP rotation, yet a genuinely "
            "confused user is back in after a coffee break; a successful login "
            "before the threshold resets the streak."
        ),
        json_schema_extra={
            "restart_required": "gateway",
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
