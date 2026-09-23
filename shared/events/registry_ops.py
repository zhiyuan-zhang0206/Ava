"""Operations, gateway, and log event declarations."""

from shared.events.payloads import (
    IdleWake,
    LlmRetry,
    PauseLifecycleWait,
    SilentIdle,
    UpdateStragglerReaped,
    UpdateStragglerReapSettled,
)
from shared.events.registry import _audit as _audit
from shared.events.registry import _telemetry as _telemetry
from shared.events.registry import _telemetry_audit as _telemetry_audit
from shared.events.system import (
    AgentRegistry,
    ArchiveFetchDegraded,
    Auth401Rejected,
    CheckpointTableSizes,
    ConvergeFilePreserved,
    EventClassReopened,
    EventSpec,
    FleetGraphStale,
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
    StatsDashboardStale,
    TelemetryReadRecovered,
    TelemetryReadStale,
    WatchdogTick,
)

_EVENTS_OPS: dict[str, EventSpec] = {
    # pause / rollout lifecycle
    "pause_lifecycle_wait": _telemetry(
        "pause_lifecycle_wait",
        "preparation bounded-waited in-flight work it did not author",
        payload=PauseLifecycleWait,
        tier="anomaly",
    ),
    # straggler reap (task #4016): the drain truncates + releases an un-landed
    # cohort member; the successor boot/resume settles the mark.
    "update_straggler_reaped": _telemetry(
        "update_straggler_reaped",
        "drain reaped straggler cohort agent(s) past their restart window",
        payload=UpdateStragglerReaped,
        tier="anomaly",
    ),
    "update_straggler_reap_settled": _telemetry(
        "update_straggler_reap_settled",
        "successor boundary settled stranded straggler-reap marks",
        payload=UpdateStragglerReapSettled,
        tier="anomaly",
    ),
    # The reap's quiet close (tasks #4164/#4156): the truncated turn/wake stops
    # through the classification instead of the crash path — the mark is a
    # deliberate truncation, not an ownership loss.
    "host_turn_truncated": _telemetry(
        "host_turn_truncated",
        "the update drain's straggler reap ended this hosted turn on purpose — the "
        "row was CAS-marked 'restarting' mid-turn and the turn's fail-closed guard "
        "read refused; no corpse marker, no error event, no failure receipt. The "
        "successor boundary settles the mark and re-delivers the claimed work",
        tier="observation",
    ),
    "host_held_wake_truncated": _telemetry(
        "host_held_wake_truncated",
        "a held-controls wake stopped quietly because the update straggler reap had "
        "marked its row 'restarting' — the successor boundary owns the row and its "
        "un-applied restart, so the wake had nothing left to do; not a failure",
        tier="observation",
    ),
    # The force-termination quiet close (task #4180): an externally commanded
    # force terminate (delivery-watchdog wedge recovery / CLI force / machine
    # pause) of the turn's own incarnation stops through the classification
    # instead of the crash path — the applied command awaits its observation
    # by the pump's own boundary; no corpse marker, no error event, no
    # failure receipt.
    "host_turn_force_terminated": _telemetry(
        "host_turn_force_terminated",
        "this hosted turn ended on its own incarnation's applied force terminate "
        "(e.g. the delivery watchdog's hosted-turn wedge recovery): the terminate "
        "command was applied but not yet observed, the turn's fail-closed guard "
        "read refused, and the pump's own boundary observes the command; not a "
        "failure",
        tier="observation",
    ),
    "host_held_wake_force_terminated": _telemetry(
        "host_held_wake_force_terminated",
        "a held-controls wake stopped quietly because its incarnation's applied "
        "force terminate landed — the pump's boundary owns the command's "
        "observation, so the wake had nothing left to do; not a failure",
        tier="observation",
    ),
    # managed-writer mode (task #4121): the enable-point decision's refusal
    # marker. `blocked` emits once per blocked decision (the rollout still runs
    # the legacy flow). Mode transitions are not event-carried -- they are
    # reconstructed from the audited config write (`env_write`: old and new
    # value plus the actor) plus the per-rollout telemetry `managed_writer`
    # field (all three states, including off).
    "managed_writer_blocked": _audit(
        "managed_writer_blocked",
        "managed-writer mode requested but refused entry: a readiness guard is missing or not True; the rollout ran the legacy flow",
    ),
    # db resilience
    "schema_mismatch_blocked": _telemetry(
        "schema_mismatch_blocked",
        "watchdog held back DB-dependent services for a code/schema/pin mismatch",
        tier="anomaly",
    ),
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
    "lgtm_dashboard_render_failed": _telemetry(
        "lgtm_dashboard_render_failed",
        "ava-ops dashboard render failed during converge; the previous provisioning file was kept",
        tier="anomaly",
    ),
    "converge_file_preserved": _telemetry(
        "converge_file_preserved",
        "converge kept a locally modified destination instead of overwriting — the current "
        "content no longer matches the recorded render; repeats every converge until resolved",
        payload=ConvergeFilePreserved,
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
    # root supervisor self-check (P7 W1.2b, task #3338) — the root tree's own
    # chain episodes and restart breaker, named distinctly from the watchdog
    # era so the two layers stay attributable during the transition
    "root_chain_broken": _telemetry(
        "root_chain_broken",
        "root self-check found a managed unit no longer a live child of the root process — one alert per episode, held until intact",
        tier="anomaly",
    ),
    "root_restart_breaker_open": _telemetry(
        "root_restart_breaker_open",
        "root health monitor restart breaker opened — repeated non-alive probe rounds held until a probe-alive round",
        tier="anomaly",
    ),
    # permissions helper healthcheck (task #3393) — the launchd-owned helper's
    # LWCR-class detection and repair escalation (F5 findings section 6)
    "permissions_helper_unhealthy": _telemetry(
        "permissions_helper_unhealthy",
        "permissions helper failed its healthcheck (ping plus launchd job classification) — one alert per episode, held until a ping-alive round",
        tier="anomaly",
    ),
    "permissions_helper_repair_failed": _telemetry(
        "permissions_helper_repair_failed",
        "permissions helper launchd repair (bootout+bootstrap) did not restore ping — escalating; the episode retries under backoff",
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
    "compact_boundary_stamp": _telemetry(
        "compact_boundary_stamp",
        "compact boundary stamp failed (segment anchor not recorded)",
        tier="noise",
    ),
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
    "fleet_graph_stale": _telemetry(
        "fleet_graph_stale",
        "the fleet-graph route served the stale/last-good graph after a degraded "
        "upstream read — one event per degradation episode, not per poll",
        payload=FleetGraphStale,
        tier="anomaly",
    ),
    "stats_dashboard_stale": _telemetry(
        "stats_dashboard_stale",
        "the stats-dashboard route served its last-good response after a failed "
        "recompute — one event per degradation episode, not per poll",
        payload=StatsDashboardStale,
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
    "chrome_page_ttl_expired": EventSpec(
        name="chrome_page_ttl_expired",
        category="log",
        tier="observation",
        doc="the browser-mcp TTL sweep closed a Chrome page whose hard deadline passed; attributes carry page_id, url, agent_id (None when no affinity slot still named the page)",
    ),
    "chrome_page_ttl_renewed": _telemetry(
        "chrome_page_ttl_renewed",
        "Chrome page TTL deadline renewed via the renew_page tool; attributes carry page_id, ttl_s, new_expires_at",
        tier="observation",
    ),
    "watcher_reaped": EventSpec(
        name="watcher_reaped",
        category="log",
        tier="observation",
        doc="the gateway TTL reaper reclaimed a watcher session — its deadline passed, or its owner agent is terminated for good; attributes carry agent_id, session_id, mode (killed / absent / machine_absent)",
    ),
    "lifecycle_pointer_done_torn": EventSpec(
        name="lifecycle_pointer_done_torn",
        category="log",
        tier="anomaly",
        doc="the gateway TTL reaper's scan found lifecycle command(s) sitting at done while agents_meta.lifecycle_command_id still pointed at them (an out-of-band torn write, task #3678) — every resurrect of the named agent(s) defers until settled; attributes carry count and samples",
    ),
    "lifecycle_fences_settled_absent_machine": EventSpec(
        name="lifecycle_fences_settled_absent_machine",
        category="log",
        tier="observation",
        doc="the gateway TTL reaper settled applied-but-unobserved force-terminate command(s) whose agent's home machine is absent from the machines registry (a decommissioned machine never runs the boot recovery that would observe its fences, task #4143); attributes carry count and samples",
    ),
}
