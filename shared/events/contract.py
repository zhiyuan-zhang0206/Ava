"""Event contract registry — the single source of truth for event names (R2-C).

Design: design-r2/design-concept.md §4.3 + okf/design/r2-single-source-of-truth (C).

``EVENTS`` is one ``EventSpec`` per event name (the ``events`` table's
``event_name`` column, OTel LogRecord semantics): writers add one entry;
producers emit through ``shared.telemetry.emit`` (fail-fast on unregistered
names); readers consume payload keys through the derived SQL fragment
constants (a hand-written ``attributes->>'...'`` literal elsewhere fails the
SQL-key lint); shared/events/registry.md is generated from this module.

Derived views live here and nowhere else: ``category_for_kind``,
``telemetry_events``, ``family_events``, ``payload_keys``, ``retention_days``,
event tiers, plus the folded ``_LLM_ERROR_EVENTS`` family and the ops grid
constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, LiteralString, NotRequired, TypedDict, get_type_hints

Category = Literal["audit", "telemetry", "log"]
EventTier = Literal["business", "anomaly", "observation", "noise"]

# Event tiers control the human-facing event stream, independently from the
# category that controls retention and access semantics:
#
# - business: an audit fact a human normally performed or requested;
# - anomaly: a warning/error or a problem-shaped signal needing attention;
# - observation: useful runtime progress that is folded by default; and
# - noise: implementation-detail telemetry retained for debugging.
#
# ``tier_for`` below applies the row-level priority: warning+ levels always
# win, then audit category, then the declared name. This lets ``status_change``
# remain one registry entry while its audit rows are business and its telemetry
# rows are noise.

# The ops-monitor bucket grid (the Insights Ops panel): 60s buckets on a fixed
# origin, shared by the LGTM reader (gateway/ops_series_lgtm.py) and the
# frontend's expectation that bucket boundaries never shift with the query
# time. OPS_BUCKET_S is the finest window step; coarser windows are multiples.
OPS_BUCKET_S = 60
OPS_GRID_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)

# Retention floors by category (registry.md §1): audit 365d+, telemetry 90d,
# log 30d. The events-maintenance TTL job consumes these through the registry.
RETENTION_BY_CATEGORY: dict[Category, int] = {"audit": 365, "telemetry": 90, "log": 30}

# The LLM failure family — one declaration; the ops panels / rollups that used
# to carry three hand-copied `_LLM_ERROR_EVENTS` tuples read `family_events`.
LLM_ERROR_FAMILY = "LLM_ERROR"


# --- payload TypedDicts (structured attribute contracts) --------------------
# Typed keys are the SQL-injection surface (`payload_keys`): a reader cannot
# reference a key no producer declared.


class LlmUsage(TypedDict):
    """`llm_usage` payload — agent/observe.py:log_llm_usage.

    ``cost_usd`` / ``price_miss`` / ``price_hit`` / ``price_out`` are the
    usage-time price snapshot (user principle: cost is billed incrementally
    with the price in force at the call, never re-priced against the current
    registry). ``cost_usd`` is the call's USD cost at the snapshot rates;
    the three rates are USD per 1M tokens (cache miss / cache hit / output).
    All four are absent on rows written before the snapshot shipped, and on
    calls of a model with no known price (a row never carries a null cost —
    absent means unpriced).

    ``calls`` is the constant 1 — it exists so the OTLP mapping mints
    ``ava_llm_usage_calls_total`` (per-agent/per-model call counts come from a
    Counter, not from a histogram's count, which drops the agent_id key).
    ``unpriced`` is 1 exactly when the price snapshot is absent (so unpriced
    call volume is countable in Prometheus); it is omitted on priced calls."""

    model: str
    calls: int
    in_total: int
    out_total: int
    cache_read: int
    reasoning: int
    latency_ms: float | None
    decode_ms: float | None
    cost_usd: float | None
    price_miss: float | None
    price_hit: float | None
    price_out: float | None
    unpriced: int | None


class TurnEnd(TypedDict):
    """`turn_end` payload — agent/graph/_llm.py."""

    ok: bool
    duration_seconds: float


class LlmProviderError(TypedDict):
    """`llm_provider_error` payload — agent/graph/_llm_errors.py.

    One row per classified provider failure — every class, so a postmortem sees
    the retried transients too; ``fatal`` says whether this one aborted the turn.

    ``billing`` is the discriminator the billing/quota alert keys on: True when
    the provider said the key is out of credit or its quota is exhausted (HTTP
    402, or a per-vendor string in the response body's ``error.type`` OR
    ``error.code`` — the vocabulary lives in ``shared/lm/errors.py``, so a new
    provider plugs in there and this key and the alert follow with no further
    wiring, wherever the vendor puts the specific reason; that module's comment
    carries the caveats). It is deliberately independent of ``error_class``: one
    vendor says it with a permanent 402, another with a transient 429, and a
    human has to clear it either way.

    ``error_type`` stays the body's ``error.type`` alone. ``error.code`` is read
    for the ``billing`` predicate and not reported here: on the vendors that
    send both, ``type`` is the broad class and ``code`` the specific reason, and
    folding the two into one reported field would change what this key means for
    every provider that already says everything through ``type``.

    ``vendor`` is the model's provider key (deepseek / claude / …, None for an
    unregistered prefix) and ``model`` the model in force at the call — the
    alert names both. ``provider`` is only the SDK package that raised
    (anthropic / openai), which DeepSeek and Claude share, so it cannot answer
    "whose key is dead".
    """

    error_class: str  # transient | permanent | unknown
    provider: str
    status: int | None
    error_type: str | None
    fatal: bool
    billing: bool
    vendor: str | None
    model: str


class ExecPayload(TypedDict):
    """`exec` / `code` payload — agent/graph/_exec.py."""

    body: str
    ok: bool
    duration_seconds: float


class ExecFailed(TypedDict):
    """`exec_failed` payload."""

    exc_type: str
    body: str


class ExecEnvelope(TypedDict):
    """`exec_envelope` payload — request/result transfer cost."""

    envelope: Literal["request", "result"]
    op: Literal["read", "write"]
    size_bytes: int
    serialize_ms: float


class ExecSubprocessKilled(TypedDict):
    """`exec_subprocess_killed` payload — a child survived the signal grace
    and the parent SIGKILLed its process group."""

    pid: int
    grace: float


class Halt(TypedDict):
    """`halt` payload — compact/idle detection reads the body."""

    body: str


class SyntaxFix(TypedDict):
    """`syntax_fix` payload."""

    fixes: str


class SseDrop(TypedDict):
    """`sse_drop` payload — kind is live data (the ops panel reads it)."""

    kind: str  # publish_error | queue_full
    n: int


class EventLogDrop(TypedDict):
    """`event_log_drop` payload."""

    n: int


class SdkCall(TypedDict):
    """`sdk_call` payload — agent/sdk_metering.py recorder."""

    fn: str
    duration: float
    sample_rate: int


class PluginActivation(TypedDict):
    """`plugin_activation` payload — shared/plugin_activation.py.

    ``plugin`` / ``surface`` / ``identifier`` are the same triple
    ``shared.plugin_contributions.Contribution`` stores, so the registration
    ledger and these runtime records join on three strings. ``detail`` is free
    text about the one firing; ``model`` is the model in force, which is what
    makes philosophy §6's per-model obsolescence gauge answerable."""

    plugin: str
    surface: str
    identifier: str
    detail: str
    model: str


class ServiceStarted(TypedDict):
    """`service_started` payload — shared/log.py."""

    name: str
    pid: int


# Functional TypedDict: the producer writes the literal key ``"from"`` (a
# Python keyword, unusable in class-syntax TypedDict fields). The class-syntax
# ``from_`` spelling made the SQL-key derivation read ``attributes->>'from_'``
# — a key that never exists — while the producer wrote ``"from"`` (audit
# 2026-08-08 P2: the registry itself drifting). This form declares the real
# wire key so `_sql_keys`/`payload_keys` derive ``attributes->>'from'``.
StatusChange = TypedDict("StatusChange", {"from": str, "to": str})


class IdleWake(TypedDict):
    """`idle_wake` payload."""

    degraded: bool
    elapsed_s: float
    rounds: int
    timeout_s: float


class ComputerAction(TypedDict):
    """`computer_action` payload — services/computer/mcp_daemon.py.

    One row per executed-or-refused desktop action. The daily quota reads
    exactly this event name (count by agent_id since local midnight), so the
    payload stays a plain bag: the counter branches on the event_name column,
    never on these keys.
    """

    action: str  # snapshot | click | type | key | scroll | window_info | session_info
    app: str | None  # frontmost window owner at action time, when known
    outcome: str  # ok | denied | error
    error: str | None  # denial/error reason; None on success
    coords: str | None  # compact "x,y" / "x,y,w,h" / key code — for audit replay
    path: str | None  # snapshot PNG path (snapshot actions only) — trace replay
    task_id: int | None  # originating task, when the call carried one


class ComputerSessionStart(TypedDict):
    """`computer_session_start` payload — services/computer/task_sessions.py.

    The envelope opening for a task's desktop actions: the first call carrying
    a task_id emits this; the matching end follows when the task goes idle.
    """

    task_id: int
    first_tool: str  # the tool of the first action in the session
    first_action_at: str  # ISO-8601 UTC


class ComputerSessionEnd(TypedDict):
    """`computer_session_end` payload — services/computer/task_sessions.py.

    The envelope closing: emitted lazily when a task_id sees no action for the
    idle threshold (outcome=idle_timeout), on the next audited call.
    """

    task_id: int
    action_count: int  # actions counted in the session, including the first
    first_action_at: str  # ISO-8601 UTC
    last_action_at: str  # ISO-8601 UTC
    outcome: str  # idle_timeout (explicit end is a phase-3 candidate)


class Spawn(TypedDict):
    """`spawn` payload (audit)."""

    machine: str
    fork_from: int | None
    fork_checkpoint: str | None


class AgentSpawned(TypedDict):
    """`agent_spawned` payload — ops/agent_spawn.py."""

    spawner: str  # "user" | "agent:<id>" | "scheduler" | ...
    forked_from: int | None


class NodeExitEntry(TypedDict):
    """One node's exit inside an aggregated per-turn `node_exit` event."""

    node: str
    outcome: str  # ok | cancelled
    duration_seconds: float


class NodeExit(TypedDict):
    """`node_exit` payload — one aggregated event per graph turn (agent/graph/_node_log.py)."""

    count: int
    nodes: list[NodeExitEntry]


class HeartbeatPaused(TypedDict):
    """`heartbeat_paused` payload — ava/self.py."""

    duration_s: float


class HeartbeatNudged(TypedDict):
    """`heartbeat_nudged` payload — services/heartbeat/daemon.py."""

    idle_minutes: int


class TaskReminderDigest(TypedDict):
    """`task_reminder_digest` payload — task-maintenance daemon."""

    owner_id: int
    task_count: int
    task_ids: list[int]


class TaskEscalation(TypedDict):
    """`task_escalation` payload — task-maintenance daemon."""

    owner_id: int
    task_count: int
    task_ids: list[int]
    leg: Literal["delegator", "user"]


class DeliveryStalled(TypedDict):
    """`delivery_stalled` payload — services/delivery_watchdog/daemon.py."""

    inbound_id: int
    age_s: float


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


class TaskUpdate(TypedDict):
    """`task_update` payload — task_registry.py; `status` only when changed."""

    status: str


class ProcessExit(TypedDict):
    """`process_exit` payload — agent/loop.py."""

    reason: str  # normal | signal:<name> | exception:<Type>
    pid: int


class AgentBootFailed(TypedDict):
    """`agent_boot_failed` payload — agent/loop.py."""

    model: str
    error_type: str
    error: str


class RecallFilter(TypedDict):
    """`recall_filter` payload — _memory_filter.py; `body` = verdict text."""

    body: str


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


@dataclass(frozen=True)
class EventSpec:
    """One declared event: name x category x payload x retention x destination.

    ``extra_categories``: a name that genuinely carries more than one category
    (status_change: the loguru side emits telemetry, audit_events emits audit).

    ``tier``: the default human-facing event tier. ``tier_for`` may override
    it for an anomaly level or an audit row. ``destination``: ``"events"``
    (default — lands in the ``events`` table) or
    ``"file"`` (log-file only, e.g. ``node_enter`` after PR #1758's sink
    filter). ``family`` groups events the ops panels / rollups treat as one
    family (e.g. LLM_ERROR). ``doc`` is the one-line registry.md description.
    """

    name: str
    category: Category
    tier: EventTier
    extra_categories: frozenset[Category] = frozenset()
    payload: Any | None = None
    retention_days: int | None = None  # None -> RETENTION_BY_CATEGORY[category]
    destination: Literal["events", "file"] = "events"
    family: str | None = None
    doc: str = ""


def _audit(name: str, doc: str, *, payload: Any | None = None) -> EventSpec:
    return EventSpec(name=name, category="audit", tier="business", payload=payload, doc=doc)


def _telemetry_audit(name: str, doc: str, *, payload: Any | None = None) -> EventSpec:
    """A name that genuinely carries both categories (status_change: the
    loguru side emits telemetry, audit_events emits audit)."""
    return EventSpec(
        name=name,
        category="telemetry",
        tier="noise",
        extra_categories=frozenset({"audit"}),
        payload=payload,
        doc=doc,
    )


def _telemetry(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    family: str | None = None,
    destination: Literal["events", "file"] = "events",
    tier: EventTier = "observation",
) -> EventSpec:
    return EventSpec(
        name=name,
        category="telemetry",
        tier=tier,
        payload=payload,
        family=family,
        destination=destination,
        doc=doc,
    )


EVENTS: dict[str, EventSpec] = {
    # ── audit (category=audit, 17) — registry.md §2, append-only operations ──
    "spawn": _audit("spawn", "new agent born", payload=Spawn),
    "fork": _audit("fork", "agent forked from another"),
    "send_message": _audit("send_message", "message sent to an agent"),
    "terminate": _audit("terminate", "agent terminated"),
    "restart": _audit("restart", "agent restart initiated"),
    "cancel": _audit("cancel", "in-flight turn cancelled"),
    "resurrect": _audit("resurrect", "terminated agent woken"),
    "restart_completed": _audit("restart_completed", "restart finished"),
    "compact": _audit("compact", "agent context compacted"),
    "circuit_breaker": _audit(
        "circuit_breaker",
        "heartbeat circuit breaker opened — a permanent provider rejection stopped "
        "heartbeat re-fires (context_overflow reason arms the forced-compact self-rescue)",
    ),
    "report_activity": _audit("report_activity", "activity report"),
    "status_change": _telemetry_audit(
        "status_change",
        "agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name",
        payload=StatusChange,
    ),
    "exit": _audit("exit", "agent process exited"),
    "label_change": _audit("label_change", "agent label changed"),
    "skill_invoked": _audit("skill_invoked", "skill invoked by an agent"),
    "task_create": _audit("task_create", "task created"),
    "task_update": _audit("task_update", "task updated", payload=TaskUpdate),
    "report_breached": _audit("report_breached", "guarantee report breached"),
    "computer_action": _audit(
        "computer_action",
        "computer-use desktop action (executed or refused)",
        payload=ComputerAction,
    ),
    "computer_session_start": _audit(
        "computer_session_start",
        "computer-use task session opened (first action with a task_id)",
        payload=ComputerSessionStart,
    ),
    "computer_session_end": _audit(
        "computer_session_end",
        "computer-use task session closed (idle timeout)",
        payload=ComputerSessionEnd,
    ),
    "mcp_tool_call": _audit(
        "mcp_tool_call",
        "MCP tool invoked through the gateway /mcp endpoint (client-scoped, args redacted)",
    ),
    # ── telemetry (category=telemetry) — registry.md §3 ──
    # frontend user modeling
    "frontend_interaction": _telemetry(
        "frontend_interaction",
        "tracked frontend interaction (click / page view / settings change)",
        payload=FrontendInteraction,
        tier="noise",
    ),
    # turn lifecycle
    "llm_usage": _telemetry("llm_usage", "LLM call metering", payload=LlmUsage),
    "turn_end": _telemetry("turn_end", "one turn finished", payload=TurnEnd),
    "llm_turn_aborted": _telemetry(
        "llm_turn_aborted", "turn aborted after retries", family=LLM_ERROR_FAMILY, tier="anomaly"
    ),
    "compact_turn_aborted": _telemetry(
        "compact_turn_aborted", "turn aborted because compaction failed", tier="anomaly"
    ),
    "llm_provider_error": _telemetry(
        "llm_provider_error",
        "LLM provider failure",
        payload=LlmProviderError,
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
    ),
    "stream_stalled_retry": _telemetry(
        "stream_stalled_retry", "stream stalled, retried", family=LLM_ERROR_FAMILY, tier="anomaly"
    ),
    "stream_overloaded_retry": _telemetry(
        "stream_overloaded_retry",
        "stream overloaded, retried",
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
    ),
    "thinking_block_sanitized": _telemetry(
        "thinking_block_sanitized", "thinking block sanitized", tier="noise"
    ),
    "multiple_tool_calls_merged": _telemetry(
        "multiple_tool_calls_merged", "concurrent tool calls merged"
    ),
    "llm_cancelled": _telemetry("llm_cancelled", "LLM call cancelled", tier="anomaly"),
    # exec lifecycle
    "exec": _telemetry("exec", "execute_code succeeded", payload=ExecPayload),
    "exec_failed": _telemetry(
        "exec_failed", "execute_code failed", payload=ExecFailed, tier="anomaly"
    ),
    "plugin_load_failed": _telemetry(
        "plugin_load_failed",
        "enabled plugin skipped because it failed to load (fail-soft)",
        payload=PluginLoadFailed,
        tier="anomaly",
    ),
    "exec_envelope": _telemetry(
        "exec_envelope",
        "exec envelope transfer cost (size + serialize time) — request snapshot / result delta",
        payload=ExecEnvelope,
    ),
    "exec_cancelled": _telemetry("exec_cancelled", "execute_code cancelled", tier="anomaly"),
    "exec(timeout)": _telemetry(
        "exec(timeout)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(failed)": _telemetry(
        "exec(failed)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(cancelled)": _telemetry(
        "exec(cancelled)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec(thread-stuck)": _telemetry(
        "exec(thread-stuck)", "historical parenthesized name (migration target)", tier="anomaly"
    ),
    "exec_timeout": _telemetry("exec_timeout", "execute_code timed out", tier="anomaly"),
    "exec_node_timeout": _telemetry("exec_node_timeout", "node-level timeout", tier="anomaly"),
    "exec_subprocess_killed": _telemetry(
        "exec_subprocess_killed",
        "exec child survived the signal grace period and was SIGKILLed",
        payload=ExecSubprocessKilled,
        tier="anomaly",
    ),
    # hosted runner (future/infra/agent-runner-as-server.md) — the dispatcher
    # that turns an inbound wake into a turn task, and the turn tasks it runs
    "host_dispatcher_subscribed": _telemetry(
        "host_dispatcher_subscribed",
        "hosted dispatcher subscribed to the inbound wake pattern",
        tier="noise",
    ),
    "host_dispatcher_reconnect": _telemetry(
        "host_dispatcher_reconnect",
        "hosted dispatcher's wake subscription dropped — reconnecting (wakes published "
        "while down are lost; the delivery watchdog re-publish covers them)",
        tier="noise",
    ),
    "host_dispatcher_bad_channel": _telemetry(
        "host_dispatcher_bad_channel",
        "hosted dispatcher ignored a wake whose channel name carried no agent id",
        tier="anomaly",
    ),
    "host_turn_crashed": _telemetry(
        "host_turn_crashed",
        "a hosted turn task raised — the task is dropped and the next wake retries "
        "from the checkpoint; neighbours are unaffected",
        tier="anomaly",
    ),
    "host_agent_prepared": _telemetry(
        "host_agent_prepared",
        "the host built an agent's per-agent runtime (chat model + startup reconcile) "
        "on a cold path — carries duration_ms and a reason of cold / config_changed / "
        "evicted, so a wake that pays the cold cost is distinguishable from one that "
        "does not, and a cache thrashing on config churn is visible as reason mix",
        tier="noise",
    ),
    "host_started": _telemetry(
        "host_started",
        "the hosted agent-runner finished process-scope boot and its dispatcher is live",
        tier="noise",
    ),
    "host_turn_uncancellable": _telemetry(
        "host_turn_uncancellable",
        "a hosted turn did not unwind after being cancelled — it is blocked where asyncio "
        "cannot interrupt it (a C call), so the host stopped waiting and exited. Carries the "
        "agent, how long the cancel was pending (waited_s), and the agent's real activity "
        "clock (last_active_at / idle_s from agents_meta, NOT the /api/agents field of the "
        "same name, which is MAX(inbound_messages.created_at) and goes stale during long "
        "turns — issue #183) so a slow shutdown is distinguishable from a genuine wedge. The "
        "turn resumes from its checkpoint on restart. Process mode had no equivalent because "
        "SIGKILL always lands",
        tier="anomaly",
    ),
    # node / process lifecycle
    "node_enter": _telemetry(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the events table (PR #1758); log files only",
        destination="file",
        tier="noise",
    ),
    "node_exit": _telemetry("node_exit", "LangGraph node exited", payload=NodeExit, tier="noise"),
    "process_exit": _telemetry(
        "process_exit", "agent process exited", payload=ProcessExit, tier="noise"
    ),
    "service_started": _telemetry(
        "service_started", "gateway/daemon started", payload=ServiceStarted, tier="noise"
    ),
    "halt": _telemetry("halt", "turn stopped (idle/compact/system)", payload=Halt, tier="noise"),
    "agent_restarted": _telemetry("agent_restarted", "agent restarted (phase2 done)"),
    "heartbeat_nudged": _telemetry(
        "heartbeat_nudged", "heartbeat reminder", payload=HeartbeatNudged, tier="noise"
    ),
    "task_reminder_digest": _telemetry(
        "task_reminder_digest",
        "overdue-task owner digest",
        payload=TaskReminderDigest,
        tier="noise",
    ),
    "task_escalation": _telemetry(
        "task_escalation", "stalled-task escalation", payload=TaskEscalation
    ),
    "delivery_stalled": _telemetry(
        "delivery_stalled", "delivery backlog", payload=DeliveryStalled, tier="anomaly"
    ),
    "restart_cas_lost": _telemetry("restart_cas_lost", "restart CAS race lost", tier="anomaly"),
    "claim_cas_lost": _telemetry(
        "claim_cas_lost", "claim CAS race lost — another lifecycle op owns the row", tier="anomaly"
    ),
    "claim_cas_lost_exit": _telemetry(
        "claim_cas_lost_exit",
        "claim wait aborted by a lost CAS — process exiting cleanly",
        tier="anomaly",
    ),
    "idle_cas_lost": _telemetry(
        "idle_cas_lost", "idle-flip CAS race lost — degraded, not fatal", tier="anomaly"
    ),
    "boot_timing": _telemetry("boot_timing", "boot duration", tier="noise"),
    "dangling_tool_use_repaired": _telemetry(
        "dangling_tool_use_repaired", "dangling tool_use repaired", tier="anomaly"
    ),
    "agent_spawned": _telemetry("agent_spawned", "agent process started", payload=AgentSpawned),
    "agent_resurrected": _telemetry("agent_resurrected", "agent resurrected"),
    "agent_terminated": _telemetry("agent_terminated", "agent terminated"),
    "agent_hibernating": _telemetry("agent_hibernating", "agent hibernated", tier="noise"),
    "agent_swapped_in": _telemetry("agent_swapped_in", "process swapped in", tier="noise"),
    "agent_revived": _telemetry("agent_revived", "agent revived", tier="noise"),
    "respawn_phase1": _telemetry("respawn_phase1", "restart phase 1", tier="noise"),
    "respawn_phase2_launch": _telemetry(
        "respawn_phase2_launch", "restart phase 2 launch", tier="noise"
    ),
    "launch_confirm_extended": _telemetry(
        "launch_confirm_extended", "launch confirm extended", tier="noise"
    ),
    "launch_confirm_failed": _telemetry(
        "launch_confirm_failed", "launch confirm failed", tier="anomaly"
    ),
    "agent_boot_failed": _telemetry(
        "agent_boot_failed",
        "agent boot failed (process exits; crash-loop budget applies)",
        payload=AgentBootFailed,
        tier="anomaly",
    ),
    "launch_confirm_task_crashed": _telemetry(
        "launch_confirm_task_crashed", "launch confirm task crashed", tier="anomaly"
    ),
    "launch_force_terminated": _telemetry(
        "launch_force_terminated", "launch force-terminated", tier="anomaly"
    ),
    "launch_force_terminated_skipped": _telemetry(
        "launch_force_terminated_skipped", "launch force-terminate skipped", tier="noise"
    ),
    "launch_retry": _telemetry("launch_retry", "launch retried"),
    # sdk / channel health
    "sdk_call": _telemetry("sdk_call", "SDK call metering", payload=SdkCall, tier="noise"),
    "plugin_activation": _telemetry(
        "plugin_activation",
        "a plugin injection surface fired (hook / wrap / prompt section)",
        payload=PluginActivation,
        tier="noise",
    ),
    "sse_drop": _telemetry("sse_drop", "SSE event dropped", payload=SseDrop, tier="anomaly"),
    "event_log_drop": _telemetry(
        "event_log_drop", "event-pipeline row shed", payload=EventLogDrop, tier="anomaly"
    ),
    "heartbeat_paused": _telemetry("heartbeat_paused", "heartbeat paused", payload=HeartbeatPaused),
    "code": _telemetry("code", "LLM generated code block", payload=ExecPayload, tier="noise"),
    # label-fallback events kept in the registry (90d retention classification)
    "text": _telemetry("text", "LLM text output", tier="noise"),
    "syntax_fix": _telemetry(
        "syntax_fix", "syntax repair executed", payload=SyntaxFix, tier="noise"
    ),
    "inbound_reconcile": _telemetry("inbound_reconcile", "inbound reconciliation", tier="noise"),
    "screen_capture_notify_failed": _telemetry(
        "screen_capture_notify_failed", "screenshot notify failed", tier="anomaly"
    ),
    # ava.ui.serve page-restore
    "page_restore_alive": _telemetry("page_restore_alive", "page restore alive", tier="noise"),
    "page_restore_reserved": _telemetry(
        "page_restore_reserved", "page restore reserved", tier="noise"
    ),
    "page_restore_query_failed": _telemetry(
        "page_restore_query_failed", "page restore query failed", tier="anomaly"
    ),
    "page_restore_failed": _telemetry("page_restore_failed", "page restore failed", tier="anomaly"),
    "page_restore_closed": _telemetry("page_restore_closed", "page restore closed", tier="noise"),
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
    "history_dump": _telemetry(
        "history_dump", "pre-compact history dumped to workspace", tier="noise"
    ),
    "checkpoint_trim": _telemetry("checkpoint_trim", "checkpoint trimmed", tier="noise"),
    "recall_filter": _telemetry(
        "recall_filter", "memory recall filter", payload=RecallFilter, tier="noise"
    ),
    "passive_recall": _telemetry("passive_recall", "passive memory recall", tier="noise"),
    "silent_idle": _telemetry("silent_idle", "silent idle verdict", tier="noise"),
    "last_msg": _telemetry("last_msg", "last-message check", tier="noise"),
    # gateway endpoint latency metering (Task #1091): 60s aggregates emitted
    # by gateway/_latency.py — one event per (route, bucket), never per request
    "gateway_latency": _telemetry(
        "gateway_latency",
        "gateway endpoint latency — 60s aggregate per route (p50/p95/p99/max/count)",
        payload=GatewayLatency,
        tier="noise",
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
    "shell_ttl_expired": EventSpec(
        name="shell_ttl_expired",
        category="log",
        tier="observation",
        doc="the gateway TTL reaper killed a persistent shell whose declared TTL passed; attributes carry agent_id, session_id, mode",
    ),
}


# ── derived views — the only spellings consumers may use ───────────────────


TIER_BY_EVENT: dict[str, EventTier] = {name: spec.tier for name, spec in EVENTS.items()}


def tier_for(event_name: str, category: str, level: str) -> EventTier:
    """Human-facing tier for one persisted event row.

    A registered name always reads through ``TIER_BY_EVENT`` first, so a
    registry/mapping drift raises instead of silently changing the events
    page. Unknown historical names remain useful observations. The row's
    severity and category deliberately take priority over that default tier:
    warning-or-higher is an anomaly, and an audit row is a business fact.
    """
    declared = TIER_BY_EVENT[event_name] if event_name in EVENTS else None
    if level.lower() in {"warning", "error", "critical"}:
        return "anomaly"
    if category == "audit":
        return "business"
    return declared if declared is not None else "observation"


def category_for_kind(event_name: str) -> Category:
    """Declared category for `event_name`, else ``"log"`` (loguru fallback)."""
    spec = EVENTS.get(event_name)
    return spec.category if spec is not None else "log"


def telemetry_events() -> frozenset[str]:
    """Every telemetry-category event name — replaces ``_TELEMETRY_KINDS``."""
    return frozenset(name for name, spec in EVENTS.items() if spec.category == "telemetry")


def family_events(family: str) -> tuple[str, ...]:
    """Event names in `family`, declaration order — replaces the hand-copied
    ``_LLM_ERROR_EVENTS`` tuples."""
    return tuple(name for name, spec in EVENTS.items() if spec.family == family)


def payload_keys(event_name: str) -> tuple[str, ...]:
    """Declared attribute keys for `event_name` (payload TypedDict order);
    empty for untyped payloads. A key a reader needs but no producer declared
    is a contract violation, not a query detail."""
    spec = EVENTS.get(event_name)
    if spec is None or spec.payload is None:
        return ()
    return tuple(get_type_hints(spec.payload))


def retention_days(event_name: str) -> int:
    """TTL for `event_name` rows: spec override, else the category floor."""
    spec = EVENTS.get(event_name)
    if spec is None:
        return RETENTION_BY_CATEGORY["log"]
    if spec.retention_days is not None:
        return spec.retention_days
    return RETENTION_BY_CATEGORY[spec.category]


# ── SQL fragment constants — the only key spellings read sites may use ──
# One dict per payload-bearing event, derived from the payload TypedDict: a
# renamed key empties the dict and every reader fails (KeyError). A literal
# ``attributes->>'...'`` elsewhere fails the SQL-key lint.


def _sql_keys(event_name: str) -> dict[str, str]:
    """``{key: "attributes->>'key'"}`` per declared payload key."""
    return {k: f"attributes->>'{k}'" for k in payload_keys(event_name)}


LLM_USAGE_KEYS = _sql_keys("llm_usage")
TURN_END_KEYS = _sql_keys("turn_end")
EXEC_KEYS = _sql_keys("exec")
CODE_KEYS = _sql_keys("code")
EXEC_FAILED_KEYS = _sql_keys("exec_failed")
HALT_KEYS = _sql_keys("halt")
SYNTAX_FIX_KEYS = _sql_keys("syntax_fix")
SSE_DROP_KEYS = _sql_keys("sse_drop")
EVENT_LOG_DROP_KEYS = _sql_keys("event_log_drop")
DELIVERY_STALLED_KEYS = _sql_keys("delivery_stalled")
FRONTEND_INTERACTION_KEYS = _sql_keys("frontend_interaction")
SDK_CALL_KEYS = _sql_keys("sdk_call")
SERVICE_STARTED_KEYS = _sql_keys("service_started")
AGENT_SPAWNED_KEYS = _sql_keys("agent_spawned")
NODE_EXIT_KEYS = _sql_keys("node_exit")
HEARTBEAT_PAUSED_KEYS = _sql_keys("heartbeat_paused")
TASK_UPDATE_KEYS = _sql_keys("task_update")
PROCESS_EXIT_KEYS = _sql_keys("process_exit")
RECALL_FILTER_KEYS = _sql_keys("recall_filter")
GATEWAY_LATENCY_KEYS = _sql_keys("gateway_latency")
LOG_KEYS = _sql_keys("log")


def registered_payload_keys() -> frozenset[str]:
    """Every declared attribute key — the SQL-key lint's registration surface."""
    return frozenset(k for spec in EVENTS.values() for k in payload_keys(spec.name))


def sql_join(*parts: str) -> LiteralString:
    """Join static SQL fragments into one query (``LiteralString``).

    Direct ``cur.execute`` read sites build through this helper so ruff's
    S608 heuristic does not misread a registry constant as user input, and
    psycopg's injection guard stays intact. Parts must be literals or
    registry-derived constants — never request-path values.
    """
    return "".join(parts)  # type: ignore[return-value]
