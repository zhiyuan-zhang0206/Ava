"""Gateway, frontend and page-serving events."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class SseDrop(TypedDict):
    """`sse_drop` payload — kind is live data (the ops panel reads it)."""

    kind: str  # publish_error | queue_full
    n: int


class FrontendInteraction(TypedDict):
    """`frontend_interaction` payload — gateway/routers/frontend_telemetry.py.

    User-modeling telemetry from the web frontend: one row per tracked
    interaction (click on a key control, page view, user_settings change).
    `page` is the normalized route ("fleet", "control/config", ...),
    `element` the tracked interaction point ("spawn", "composer-send",
    "setting-change", ...). `key`/`value` carry the settings key and a
    sanitized scalar rendering of its new value on setting-change events
    only. `session_id` is the per-tab uuid the frontend minted — it groups
    one browser session without carrying any identity data.
    """

    page: str
    element: str
    session_id: str
    key: str | None
    value: str | None


class GatewayLatency(TypedDict):
    """`gateway_latency` payload — gateway/middleware/latency.py 60s aggregator.

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
    """`auth401_rejected` payload — gateway/auth/rejection_log.py flusher.

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
    """`agent_registry` payload — services/events_maintenance/registry_gauge.py 60s loop.

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


# The closed reason vocabulary of `fleet_graph_stale` (task #3925): every
# stale-serving fallback on GET /api/fleet/graph names WHY it degraded — a
# canceled database read or a phase crossing the route budget. Keep the set closed: the alert rule and dashboards rely on
# it.
FleetGraphStaleReason = Literal[
    "pg_timeout",
    "pg_budget",
]


class FleetGraphStale(TypedDict):
    """`fleet_graph_stale` payload — one degraded fleet-graph serving episode.

    Emitted when GET /api/fleet/graph serves the stale/last-good graph
    because an upstream read (Postgres / Prometheus) failed, was refused, or
    crossed the route budget. One event per degradation episode, not per poll:
    a per-reason emission rate cap bounds retry-storm floods (task #3925, user
    ruling 2026-09-18).
    """

    route: str
    reason: FleetGraphStaleReason


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


EVENTS: dict[str, EventSpec] = {
    # frontend user modeling
    "frontend_interaction": telemetry_event(
        "frontend_interaction",
        "tracked frontend interaction (click / page view / settings change)",
        payload=FrontendInteraction,
        tier="noise",
        site='gateway/routers/frontend_telemetry.py telemetry.emit("telemetry", ...)',
    ),
    "sse_drop": telemetry_event("sse_drop", "SSE event dropped", payload=SseDrop, tier="anomaly"),
    # ava.ui.serve page-restore
    "page_restore_alive": telemetry_event("page_restore_alive", "page restore alive", tier="noise"),
    "page_restore_reserved": telemetry_event(
        "page_restore_reserved", "page restore reserved", tier="noise"
    ),
    "page_restore_query_failed": telemetry_event(
        "page_restore_query_failed", "page restore query failed", tier="anomaly"
    ),
    "page_restore_failed": telemetry_event(
        "page_restore_failed", "page restore failed", tier="anomaly"
    ),
    "page_restore_closed": telemetry_event(
        "page_restore_closed", "page restore closed", tier="noise"
    ),
    "page_restore_notified": telemetry_event(
        "page_restore_notified", "page restore notified", tier="noise"
    ),
    # gateway endpoint latency metering (Task #1091): 60s aggregates emitted
    # by gateway/middleware/latency.py — one event per (route, bucket), never per request
    "gateway_latency": telemetry_event(
        "gateway_latency",
        "gateway endpoint latency — 60s aggregate per route (p50/p95/p99/max/count)",
        payload=GatewayLatency,
        tier="noise",
        site=('gateway/middleware/latency.py:emit_bucket telemetry.emit("telemetry", ...)'),
    ),
    "sse": telemetry_event(
        "sse",
        "gateway SSE lifecycle — active connections by mode plus open/close counters",
        payload=SseLifecycle,
        tier="noise",
        site=("gateway/middleware/runtime_metrics.py:sse_opened/sse_closed positional emit"),
    ),
    "gateway_process": telemetry_event(
        "gateway_process",
        "gateway process CPU, resident memory, and open file descriptors (60s sample)",
        payload=GatewayProcess,
        tier="noise",
        site="gateway/middleware/runtime_metrics.py:_emit_snapshot positional emit",
    ),
    "gateway_event_loop": telemetry_event(
        "gateway_event_loop",
        "gateway event-loop maximum callback lag and slow ticks (60s window)",
        payload=GatewayEventLoop,
        tier="noise",
        site="gateway/middleware/runtime_metrics.py:_emit_snapshot positional emit",
    ),
    # gateway auth middleware 401 aggregate (task #1712) — one event per 60s
    # window, never per rejection: the per-request line is DEBUG/throttled on
    # purpose (PR #665), but the central count must stay observable.
    "auth401_rejected": telemetry_event(
        "auth401_rejected",
        "gateway auth-401 rejections in the 60s window (aggregate count)",
        payload=Auth401Rejected,
        tier="noise",
        site=('gateway/auth/rejection_log.py:emit_auth401_count telemetry.emit("telemetry", ...)'),
    ),
    # agent registry max id (task #2010) — one absolute gauge sample per 60s
    # window, never a counter: the registry high-water mark is state, not a sum.
    "agent_registry": telemetry_event(
        "agent_registry",
        "agent registry max id — the agents-table high-water mark (absolute state, 60s sample)",
        payload=AgentRegistry,
        tier="noise",
        site=(
            'services/events_maintenance/registry_gauge.py:emit_max_agent_id telemetry.emit("telemetry", ...)'
        ),
    ),
    # memory search store stats (row-growth monitoring, task #2088) — one
    # absolute gauge sample per 60s window: row count + last npz save
    # duration, never counters.
    "memory_search_stats": telemetry_event(
        "memory_search_stats",
        "memory search store rows + last save duration (absolute state, 60s sample)",
        payload=MemorySearchStats,
        tier="noise",
        site=("services/memory_search/app.py:emit_memory_search_stats (positional emit)"),
    ),
    # gate entry-point diagnostics
    "gate_auth_probe_failed": telemetry_event(
        "gate_auth_probe_failed",
        "gate auth probe failed — carries the classification (auth/timeout/network/application) and exception shape",
        payload=GateAuthProbeFailed,
        tier="anomaly",
    ),
    "fleet_graph_stale": telemetry_event(
        "fleet_graph_stale",
        "the fleet-graph route served the stale/last-good graph after a degraded "
        "upstream read — one event per degradation episode, not per poll",
        payload=FleetGraphStale,
        tier="anomaly",
        site="gateway/routers/fleet_graph.py:_emit_stale (positional emit)",
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
        doc="the TTL reaper terminalized a page row whose expires_at passed; attributes carry agent_id, name, page_id",
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
        doc="the TTL reaper killed a persistent shell whose declared TTL passed; attributes carry agent_id, session_id, mode",
    ),
    "chrome_page_ttl_expired": EventSpec(
        name="chrome_page_ttl_expired",
        category="log",
        tier="observation",
        doc="the browser-mcp TTL sweep closed a Chrome page whose hard deadline passed; attributes carry page_id, url, agent_id (None when no affinity slot still named the page)",
        site=('services/browser/page_lifecycle.py:reap_expired_pages telemetry.emit("log", ...)'),
    ),
    "chrome_page_ttl_renewed": telemetry_event(
        "chrome_page_ttl_renewed",
        "Chrome page TTL deadline renewed via the renew_page tool; attributes carry page_id, ttl_s, new_expires_at",
        tier="observation",
        site=(
            'services/browser/page_lifecycle.py:renew_agent_page telemetry.emit("telemetry", ...)'
        ),
    ),
}
