"""System and operations event payload schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

from shared.events.payloads import Category, EventTier, RetentionClass


class ProcessExit(TypedDict):
    """`process_exit` payload — agent/loop.py."""

    reason: str  # normal | signal:<name> | exception:<Type>
    pid: int


class AgentBootFailed(TypedDict):
    """`agent_boot_failed` payload — agent/loop.py."""

    model: str
    error_type: str
    error: str


class RecallFilter(TypedDict, total=False):
    """`recall_filter` payload — _memory_filter.py; `body` = verdict text.

    Successful verdicts add a process-keyed ``query_hmac_sha256`` (never the
    query text) and a bounded basename-only ``picked_paths`` sample. Failures
    only have ``body`` because no verdict exists to retain.
    """

    body: str
    query_hmac_sha256: str
    picked_paths: list[str]


class PassiveRecall(TypedDict, total=False):
    """`passive_recall` payload — _memory_recall.py / ava_memory plugin.

    The recall pass leg timings in milliseconds (the success path); the
    defer / deadline-skip emissions carry no timing keys.
    """

    search_ms: int
    filter_ms: int


class HookTiming(TypedDict):
    """`hook_timing` payload — agent/hooks/_registry.py.

    Per-hook wall durations in milliseconds for one hook-runner pass
    (`before_llm` / `before_exec` / `after_exec` / `after_init`) — the
    sub-span replacement attributing a slow node to its hooks.
    """

    hook_ms: dict[str, float]


class ResolvedMarker(TypedDict, total=False):
    """`warning_resolved` / `error_resolved` payload, supporting two eras.

    Legacy producers named one mutable Postgres event with `target_event_id`
    and/or `match`; those attributes remain declared so historical marker
    lines stay contract-valid. New producers declare an immutable Loki event
    class and the `event_dismissals` row that carries its resolution state.
    """

    target_event_id: NotRequired[int]
    match: NotRequired[str]
    resolved_by: NotRequired[int]
    category: NotRequired[str]
    level: NotRequired[str]
    event_name: NotRequired[str]
    source: NotRequired[str]
    agent_id: NotRequired[int | None]
    dismissed_by: NotRequired[int]
    note: NotRequired[str]


class EventClassReopened(TypedDict):
    """`warning_reopened` / `error_reopened` immutable class-state marker."""

    category: str
    level: str
    event_name: str
    source: str
    agent_id: int | None
    dismissed_by: int
    note: str
    reopened_by: str
    triggered_by_count: int | None


class ResolutionStatus(TypedDict):
    """`resolution_status` payload — absolute class-resolution gauges.

    ``unresolved_*`` are the events of actively-dismissed classes subtracted
    from the fixed window's per-class counts (the net); ``dismissed_*`` are
    the subtracted counts themselves, so the visible Grafana trio
    (total = Warning/Error tiles, dismissed, unresolved) sums by construction
    (task #1935).
    """

    unresolved_warnings: int
    unresolved_errors: int
    dismissed_warnings: int
    dismissed_errors: int
    window: str


class CheckpointTableSizes(TypedDict):
    """`checkpoint_table_sizes` payload — physical sizes + live row counts.

    Emitted hourly by the events-maintenance pass and after each blob vacuum
    run; the live counts separate live growth from dead-tuple bloat when
    reading the physical-size curve.
    """

    blobs_bytes: int
    checkpoints_bytes: int
    writes_bytes: int
    blobs_live: int
    checkpoints_live: int
    writes_live: int


class GatewayLatency(TypedDict):
    """`gateway_latency` payload — gateway/_latency.py 60s aggregator.

    One event per (route, 60s bucket) carrying p50/p95/p99/max/count — never
    per request (Task #1091).
    """

    route: str  # matched route pattern, e.g. /api/agents/{agent_id}/messages
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    count: int


class SseLifecycle(TypedDict):
    """`sse` payload — one established or closed gateway stream."""

    mode: Literal["filtered", "throttled"]
    active_connections: int
    opened: NotRequired[int]
    closed: NotRequired[int]


class GatewayProcess(TypedDict):
    """`gateway_process` payload — gateway process resource snapshot."""

    cpu_percent: float
    rss_bytes: int
    fd_count: int


class GatewayEventLoop(TypedDict):
    """`gateway_event_loop` payload — worst lag and slow ticks per window."""

    lag_ms: float
    slow_ticks: int


class Auth401Rejected(TypedDict):
    """`auth401_rejected` payload — gateway/_auth401_log.py flusher.

    One event per 60s window carrying the number of gateway auth-middleware
    401 rejections in that window (task #1712). The per-request log line was
    downgraded to DEBUG / throttled to recover the event stream from the SSE
    reconnect storm (PR #665), which removed the central count too — this
    aggregate restores the counter at bounded volume (one event per window,
    never one per rejection), feeding the OTLP-mapped
    ``ava_auth401_rejected_count_total`` Prometheus counter.
    """

    count: int


class AgentRegistry(TypedDict):
    """`agent_registry` payload — gateway/_agent_max_id.py 60s flusher.

    One event per 60s window carrying the ``agents`` table high-water mark
    (max id) — the fleet's growth curve (task #2010). Absolute state, never
    a sum: the OTLP disposition override records it as an ObservableGauge
    (``ava_agent_registry_max_id_ratio``), so a flat fleet does not accrue
    value the way a Counter would.
    """

    max_id: int


class MemorySearchStats(TypedDict):
    """`memory_search_stats` payload — services/memory_search/app.py 60s flusher.

    One event per 60s window carrying the memory-search store's absolute
    state: total chunk rows plus the duration of the most recent successful
    npz save. Both are state, never sums: the OTLP disposition override
    records them as ObservableGauges (``ava_memory_search_stats_rows_ratio``
    / ``ava_memory_search_stats_last_save_seconds``), so a flat store does
    not accrue value the way Counters would. ``last_save_seconds`` is absent
    until the first save since boot (an absent optional metric is not zero).
    """

    rows: int
    last_save_seconds: float


class WatchdogTick(TypedDict):
    """`watchdog_tick` payload — services/watchdog/daemon.py.

    One fully completed watchdog round replaces the previous timestamp. The
    OTLP metric is a gauge so Prometheus can calculate a role's tick age.
    """

    last_tick_timestamp_seconds: float


class ScheduleStalled(TypedDict):
    """`schedule_stalled` payload — gateway/schedule_manager.py.

    Emitted once after an enabled, non-completed schedule has had no live
    session for more than two hours. A live observation rearms a later outage.
    """

    schedule_id: int
    status: str
    stalled_seconds: float


class PitrRemoteInventory(TypedDict):
    """`pitr_remote_inventory` payload — retention scheduler snapshot.

    The viewer-only inventory refresh emits the backend-scoped object and byte
    footprint as absolute gauges. A remote retention delete path does not
    exist here; the planner remains dry-run-only.
    """

    backend: str
    object_count: int
    bytes: int


class RecoveryDrillFailed(TypedDict):
    """`recovery_drill_failed` payload — scheduled logical/PITR proofs.

    ``drill`` identifies the recovery path that needs intervention. ``detail``
    is the bounded failure diagnostic retained in the event stream; it is not a
    Prometheus measurement or alert grouping key.
    """

    drill: str
    detail: str


class TelemetryReadStale(TypedDict):
    """`telemetry_read_stale` payload — gateway/telemetry_staleness.py."""

    source: str
    signal: str
    threshold_s: int
    age_s: float | None
    action: str
    reason: str


class TelemetryReadRecovered(TypedDict):
    """`telemetry_read_recovered` payload — gateway/telemetry_staleness.py."""

    source: str
    signal: str
    stale_duration_s: float


class OtlpBackendDisabled(TypedDict):
    """`otlp_backend_disabled` payload — shared/telemetry_otlp.py."""

    reason: str
    endpoint: str | None


class OtlpBackendRecovered(TypedDict):
    """`otlp_backend_recovered` payload — shared/telemetry_otlp.py."""

    endpoint: str | None
    disabled_s: float | None


class LokiQueryFailed(TypedDict):
    """`loki_query_failed` payload — gateway/loki_events.py transport failure.

    One row per failed Loki HTTP call (timeout / disconnect / non-2xx) with
    the request shape, so a stalled query is attributable after Loki's own
    logs have rotated away (task #1289: the 2026-08-20 incident window).
    """

    endpoint: str
    duration_s: float
    error: str
    window_from: str | None
    window_to: str | None
    query: str


class ArchiveFetchDegraded(TypedDict):
    """`archive_fetch_degraded` payload — frozen-archive read degradation.

    One row per degraded frozen-archive read (lock-wait skip or failed scan)
    with the owning route, so a saturated Loki's effect on the tie/edge
    graphs is attributable in the event stream even though the routes now
    answer fast (fail-open) instead of surfacing the stall as slow-route
    latency (2026-08-29/30 incident — task #2004).
    """

    route: str
    reason: str


class PromQueryFailed(TypedDict):
    """`prom_query_failed` payload — gateway/prom_metrics.py transport failure."""

    endpoint: str
    duration_s: float
    error: str
    query: str


class PageServeDirMissing(TypedDict):
    """`page_serve_dir_missing` payload — page-server daemon degradation alert.

    The directory behind a served page disappeared or ceased to be a directory.
    The daemon reports the first observation and its eventual auto-close, so the
    page's key and source path remain attributable after the row is gone.
    """

    agent_id: int
    key: str
    name: str
    serve_dir: str
    port: int


class LokiQueryBudget(TypedDict):
    """One local Loki-admission transition and its post-transition state.

    Float state/wait fields become OTLP histograms; integer outcome fields are
    0/1 deltas and become counters. `outcome` is the bounded reason dimension.
    """

    outcome: Literal["queued", "acquired", "released", "queue_full", "wait_timeout", "cancelled"]
    active: float
    queued: float
    high_water: float
    wait_ms: float
    acquired: int
    queue_full: int
    wait_timeout: int


class PromQueryBudget(TypedDict):
    """One local Prometheus-admission transition and post-transition state."""

    outcome: Literal["queued", "acquired", "released", "queue_full", "wait_timeout", "cancelled"]
    active: float
    queued: float
    high_water: float
    wait_ms: float
    acquired: int
    queue_full: int
    wait_timeout: int


class GateAuthProbeFailed(TypedDict):
    """`gate_auth_probe_failed` payload — services/gate/daemon.py.

    One row per failed gateway auth probe, emitted by the gate's fail-closed
    verdict (audit #1736: probe exceptions used to collapse into an
    unobservable "down").

    ``category`` is the classification a postmortem keys on — ``auth``
    (the gateway answered 401/403), ``timeout`` (the probe's 3s budget
    elapsed), ``network`` (transport failure), or ``application`` (the
    gateway answered but not with a valid auth check, or an unexpected
    failure). ``status`` is the HTTP status when the gateway answered with
    an error, else None. ``latency_ms`` is the probe duration, including
    the timeout budget when one elapsed.
    """

    category: str
    exception_type: str
    exception_value: str
    status: int | None
    latency_ms: int


class PluginLoadFailed(TypedDict):
    """`plugin_load_failed` payload — agent/graph/_build.py.

    One row per plugin that could not be loaded during `_load_extensions`
    (its `plugin.py` raised at import, or the config referenced a plugin
    whose directory is gone). The plugin is skipped — fail-soft contract
    (2026-08-28 ava_ledger incident): a broken plugin must never block
    `import ava` / graph build for the whole cluster. This event is the loud
    half of that contract; `error` carries the exception type + message so
    ops sees which plugin broke and why.
    """

    plugin: str
    error: str


class LogPayload(TypedDict):
    """`log` payload — bare-log fallback; `msg` rides every loguru-sourced row."""

    msg: str


class HostDispatcherScanFailed(TypedDict):
    """`host_dispatcher_scan_failed` payload — agent-host durable backstop."""

    backoff_s: float


@dataclass(frozen=True)
class EventSpec:
    """One declared event: name x category x payload x destination.

    ``extra_categories``: a name that genuinely carries more than one category
    (status_change: the loguru side emits telemetry, audit_events emits audit).

    ``tier``: the default human-facing event tier. ``tier_for`` may override
    it for an anomaly level or an audit row. ``destination``: ``"events"``
    (default — lands in the ``events`` table) or
    ``"file"`` (log-file only, e.g. ``node_enter`` after PR #1758's sink
    filter). ``family`` groups events the ops panels / rollups treat as one
    family (e.g. LLM_ERROR). ``doc`` is the one-line registry.md description.

    ``retention_class``: how long this name's rows must survive (see the
    ``RetentionClass`` note above). ``None`` = undeclared, which means the
    global Loki retention applies; only ``"lineage"`` is declared today.
    """

    name: str
    category: Category
    tier: EventTier
    extra_categories: frozenset[Category] = frozenset()
    payload: Any | None = None
    destination: Literal["events", "file"] = "events"
    family: str | None = None
    doc: str = ""
    retention_class: RetentionClass | None = None
