"""Operations, gateway, and log event declarations."""

from shared.events.payloads import IdleWake, LlmRetry, SilentIdle
from shared.events.registry import _audit as _audit
from shared.events.registry import _telemetry as _telemetry
from shared.events.registry import _telemetry_audit as _telemetry_audit
from shared.events.system import (
    AgentRegistry,
    ArchiveFetchDegraded,
    Auth401Rejected,
    CheckpointTableSizes,
    EventClassReopened,
    EventSpec,
    GateAuthProbeFailed,
    GatewayEventLoop,
    GatewayLatency,
    GatewayProcess,
    HookTiming,
    LogPayload,
    LokiQueryBudget,
    LokiQueryFailed,
    MemorySearchStats,
    OtlpBackendDisabled,
    OtlpBackendRecovered,
    PageServeDirMissing,
    PassiveRecall,
    PitrRemoteInventory,
    PromQueryBudget,
    PromQueryFailed,
    RecallFilter,
    RecoveryDrillFailed,
    ResolutionStatus,
    ResolvedMarker,
    ScheduleStalled,
    SseLifecycle,
    TelemetryReadRecovered,
    TelemetryReadStale,
    WatchdogTick,
)

_EVENTS_OPS: dict[str, EventSpec] = {
    # db resilience
    "db_outage_wait": _telemetry("db_outage_wait", "db outage wait", tier="anomaly"),
    "db_outage_pause": _telemetry("db_outage_pause", "db outage pause", tier="anomaly"),
    "db_outage_reconcile_retry": _telemetry(
        "db_outage_reconcile_retry", "db outage reconcile retry", tier="anomaly"
    ),
    "db_recovered": _telemetry("db_recovered", "db recovered", tier="anomaly"),
    "db_pool_acquire_timeout": _telemetry(
        "db_pool_acquire_timeout", "db pool acquire timeout", tier="anomaly"
    ),
    "db_pool_acquire_slow": _telemetry(
        "db_pool_acquire_slow", "db pool acquire slow", tier="anomaly"
    ),
    "checkpoint_write_failed": _telemetry(
        "checkpoint_write_failed", "checkpoint write failed", tier="anomaly"
    ),
    "pgbouncer_repaired": _telemetry(
        "pgbouncer_repaired", "pgbouncer watchdog repair", tier="anomaly"
    ),
    "editable_pth_repaired": _telemetry(
        "editable_pth_repaired",
        "poisoned editable-install pointer repaired to the prod source root",
        tier="anomaly",
    ),
    "editable_direct_url_repaired": _telemetry(
        "editable_direct_url_repaired",
        "poisoned editable-install direct_url repaired to the prod source root",
        tier="anomaly",
    ),
    "exec_editable_install_poisoned": _telemetry(
        "exec_editable_install_poisoned",
        "poisoned editable install repaired before an exec child spawn",
        tier="anomaly",
    ),
    "source_tree_reset": _telemetry(
        "source_tree_reset",
        "prod source checkout reset to the installed commit / cleaned of untracked files",
        tier="anomaly",
    ),
    # labeler / trace housekeeping
    "label_generated": _telemetry("label_generated", "label auto-generated", tier="noise"),
    "label_generate_failed": _telemetry(
        "label_generate_failed", "label generation failed", tier="anomaly"
    ),
    "label_generate_skipped": _telemetry(
        "label_generate_skipped", "label generation skipped", tier="noise"
    ),
    "label_generate_empty": _telemetry(
        "label_generate_empty", "label generation empty", tier="noise"
    ),
    "label_generate_rejected": _telemetry(
        "label_generate_rejected", "label generation rejected as not a label", tier="noise"
    ),
    "label_generate_retired": _telemetry(
        "label_generate_retired",
        "label generation given up on after repeated failures",
        tier="noise",
    ),
    "trace": _telemetry("trace", "otel span export", tier="noise"),
    # agent lifecycle / state
    "idle_wake": _telemetry("idle_wake", "agent woken from idle", payload=IdleWake, tier="noise"),
    "wake_degraded": _telemetry(
        "wake_degraded",
        "RedisInboundListener wake path degraded (instant pub/sub wake off)",
        tier="anomaly",
    ),
    "wake_restored": _telemetry(
        "wake_restored",
        "RedisInboundListener wake path recovered (clean consume restored instant wake)",
        tier="noise",
    ),
    # compact / checkpoint / memory housekeeping
    "compact_request": _telemetry("compact_request", "compact requested", tier="noise"),
    "auto_compact": _telemetry("auto_compact", "auto-compact", tier="noise"),
    "compact_reminder": _telemetry("compact_reminder", "compact reminder", tier="noise"),
    # heartbeat circuit breaker (task #1928)
    "circuit_breaker_open": _telemetry(
        "circuit_breaker_open", "heartbeat circuit breaker opened", tier="noise"
    ),
    "circuit_breaker_closed": _telemetry(
        "circuit_breaker_closed", "heartbeat circuit breaker closed", tier="noise"
    ),
    "circuit_breaker_compact": _telemetry(
        "circuit_breaker_compact", "forced overflow compact fired by the open breaker", tier="noise"
    ),
    "heartbeat_circuit_open": _telemetry(
        "heartbeat_circuit_open", "heartbeat consumed while the breaker is open", tier="noise"
    ),
    "emergency_compact": _telemetry(
        "emergency_compact", "emergency compaction (overflow self-rescue)", tier="noise"
    ),
    # watchdog respawn circuit breaker (task #1941)
    "respawn_breaker_open": _telemetry(
        "respawn_breaker_open",
        "watchdog respawn circuit breaker opened — repeated failed respawns held until a probe-alive round",
        tier="anomaly",
    ),
    "schedule_stalled": _telemetry(
        "schedule_stalled",
        "enabled non-completed schedule has had no live session for more than two hours",
        payload=ScheduleStalled,
        tier="anomaly",
    ),
    "history_dump": _telemetry(
        "history_dump", "pre-compact history dumped to workspace", tier="noise"
    ),
    "checkpoint_trim": _telemetry("checkpoint_trim", "checkpoint trimmed", tier="noise"),
    "recall_filter": _telemetry(
        "recall_filter", "memory recall filter", payload=RecallFilter, tier="noise"
    ),
    "passive_recall": _telemetry(
        "passive_recall", "passive memory recall", payload=PassiveRecall, tier="noise"
    ),
    "hook_timing": _telemetry(
        "hook_timing",
        "hook-runner pass — per-hook wall durations, attributing a slow before_llm / "
        "before_exec node to its hooks from events alone",
        payload=HookTiming,
        tier="noise",
    ),
    "silent_idle": _telemetry(
        "silent_idle", "silent idle cost-boundary verdict", payload=SilentIdle, tier="noise"
    ),
    "llm_retry": _telemetry(
        "llm_retry", "LLM retry sequence completion", payload=LlmRetry, tier="observation"
    ),
    "last_msg": _telemetry("last_msg", "last-message check", tier="noise"),
    # gateway endpoint latency metering (Task #1091): 60s aggregates emitted
    # by gateway/_latency.py — one event per (route, bucket), never per request
    "gateway_latency": _telemetry(
        "gateway_latency",
        "gateway endpoint latency — 60s aggregate per route (p50/p95/p99/max/count)",
        payload=GatewayLatency,
        tier="noise",
    ),
    "sse": _telemetry(
        "sse",
        "gateway SSE lifecycle — active connections by mode plus open/close counters",
        payload=SseLifecycle,
        tier="noise",
    ),
    "gateway_process": _telemetry(
        "gateway_process",
        "gateway process CPU, resident memory, and open file descriptors (60s sample)",
        payload=GatewayProcess,
        tier="noise",
    ),
    "gateway_event_loop": _telemetry(
        "gateway_event_loop",
        "gateway event-loop maximum callback lag and slow ticks (60s window)",
        payload=GatewayEventLoop,
        tier="noise",
    ),
    # gateway auth middleware 401 aggregate (task #1712) — one event per 60s
    # window, never per rejection: the per-request line is DEBUG/throttled on
    # purpose (PR #665), but the central count must stay observable.
    "auth401_rejected": _telemetry(
        "auth401_rejected",
        "gateway auth-401 rejections in the 60s window (aggregate count)",
        payload=Auth401Rejected,
        tier="noise",
    ),
    # agent registry max id (task #2010) — one absolute gauge sample per 60s
    # window, never a counter: the registry high-water mark is state, not a sum.
    "agent_registry": _telemetry(
        "agent_registry",
        "agent registry max id — the agents-table high-water mark (absolute state, 60s sample)",
        payload=AgentRegistry,
        tier="noise",
    ),
    # memory search store stats (row-growth monitoring, task #2088) — one
    # absolute gauge sample per 60s window: row count + last npz save
    # duration, never counters.
    "memory_search_stats": _telemetry(
        "memory_search_stats",
        "memory search store rows + last save duration (absolute state, 60s sample)",
        payload=MemorySearchStats,
        tier="noise",
    ),
    "watchdog_tick": _telemetry(
        "watchdog_tick",
        "watchdog completed one full healthcheck and reconcile round",
        payload=WatchdogTick,
        tier="noise",
    ),
    "pitr_remote_inventory": _telemetry(
        "pitr_remote_inventory",
        "PITR remote object inventory (backend-scoped absolute object and byte state)",
        payload=PitrRemoteInventory,
        tier="noise",
    ),
    "recovery_drill_failed": _telemetry(
        "recovery_drill_failed",
        "scheduled logical dump or PITR recovery proof failed",
        payload=RecoveryDrillFailed,
        tier="anomaly",
    ),
    "telemetry_read_stale": _telemetry(
        "telemetry_read_stale",
        "read-side telemetry staleness detected — heartbeat older than threshold",
        payload=TelemetryReadStale,
        tier="anomaly",
    ),
    "telemetry_read_recovered": _telemetry(
        "telemetry_read_recovered",
        "read-side telemetry heartbeat recovered",
        payload=TelemetryReadRecovered,
    ),
    "otlp_backend_disabled": _telemetry(
        "otlp_backend_disabled",
        "OTLP backend disabled for this process (init failure / collector unreachable); retry scheduled",
        payload=OtlpBackendDisabled,
        tier="anomaly",
    ),
    "otlp_backend_recovered": _telemetry(
        "otlp_backend_recovered",
        "OTLP backend brought up after a disabled episode (periodic retry)",
        payload=OtlpBackendRecovered,
    ),
    "loki_query_budget": _telemetry(
        "loki_query_budget",
        "local Loki query-admission transition and capacity metrics",
        payload=LokiQueryBudget,
        tier="noise",
    ),
    "prom_query_budget": _telemetry(
        "prom_query_budget",
        "local Prometheus query-admission transition and capacity metrics",
        payload=PromQueryBudget,
        tier="noise",
    ),
    # Immutable Loki lines cannot be updated with a `resolved_by` attribute.
    # These markers record class-state transitions while `event_dismissals`
    # remains the active-resolution source of truth (task #1468).
    "warning_resolved": _telemetry(
        "warning_resolved",
        "class-level warning dismissal marker (legacy target-event attributes remain accepted)",
        payload=ResolvedMarker,
        tier="anomaly",
    ),
    "error_resolved": _telemetry(
        "error_resolved",
        "class-level error/critical dismissal marker (legacy target-event attributes remain accepted)",
        payload=ResolvedMarker,
        tier="anomaly",
    ),
    "warning_reopened": _telemetry(
        "warning_reopened",
        "class-level warning dismissal reopened manually or by the burst safety valve",
        payload=EventClassReopened,
        tier="anomaly",
    ),
    "error_reopened": _telemetry(
        "error_reopened",
        "class-level error/critical dismissal reopened manually or by the burst safety valve",
        payload=EventClassReopened,
        tier="anomaly",
    ),
    "resolution_status": _telemetry(
        "resolution_status",
        "absolute unresolved + dismissed warning/error class counts over the daemon's fixed six-hour window",
        payload=ResolutionStatus,
        tier="noise",
    ),
    "checkpoint_table_sizes": _telemetry(
        "checkpoint_table_sizes",
        "checkpoint table physical sizes and live row counts (hourly + after each blob vacuum run)",
        payload=CheckpointTableSizes,
    ),
    # gate entry-point diagnostics
    "gate_auth_probe_failed": _telemetry(
        "gate_auth_probe_failed",
        "gate auth probe failed — carries the classification (auth/timeout/network/application) and exception shape",
        payload=GateAuthProbeFailed,
        tier="anomaly",
    ),
    # ── log (category=log) — registry.md §4, the bare-log fallback ──
    "log": EventSpec(
        name="log", category="log", tier="noise", payload=LogPayload, doc="bare log line"
    ),
    "loki_query_failed": EventSpec(
        name="loki_query_failed",
        category="log",
        tier="anomaly",
        payload=LokiQueryFailed,
        doc="a Loki HTTP query failed (timeout / disconnect / non-2xx) — carries the request shape",
    ),
    "archive_fetch_degraded": _telemetry(
        "archive_fetch_degraded",
        "frozen Loki archive read degraded (lock-wait skip or failed scan)",
        payload=ArchiveFetchDegraded,
        tier="anomaly",
    ),
    "prom_query_failed": EventSpec(
        name="prom_query_failed",
        category="log",
        payload=PromQueryFailed,
        tier="anomaly",
        doc="a Prometheus HTTP query failed (timeout / disconnect / non-2xx) — carries the request shape",
    ),
    "page_serve_dir_missing": EventSpec(
        name="page_serve_dir_missing",
        category="log",
        tier="anomaly",
        payload=PageServeDirMissing,
        doc="a served page directory disappeared; emitted on degradation and auto-close",
    ),
    "page_ttl_expired": EventSpec(
        name="page_ttl_expired",
        category="log",
        tier="observation",
        doc="the gateway TTL reaper terminalized a page row whose expires_at passed; attributes carry agent_id, name, page_id",
    ),
    "page_language_lookup_failed": EventSpec(
        name="page_language_lookup_failed",
        category="log",
        tier="anomaly",
        doc="the gateway could not read the page copy language from user_settings (DB failure) and fell back to the default; attributes carry exc_type, exc_message",
    ),
    "page_proxy_502": EventSpec(
        name="page_proxy_502",
        category="log",
        tier="anomaly",
        doc="the gateway reverse proxy could not reach a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message",
    ),
    "page_proxy_504": EventSpec(
        name="page_proxy_504",
        category="log",
        tier="anomaly",
        doc="the gateway reverse proxy timed out dialing a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message",
    ),
    "shell_ttl_expired": EventSpec(
        name="shell_ttl_expired",
        category="log",
        tier="observation",
        doc="the gateway TTL reaper killed a persistent shell whose declared TTL passed; attributes carry agent_id, session_id, mode",
    ),
}
