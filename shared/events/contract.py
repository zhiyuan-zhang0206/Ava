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
plus the folded ``_LLM_ERROR_EVENTS`` family and the ops grid constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, LiteralString, TypedDict, get_type_hints

Category = Literal["audit", "telemetry", "log"]

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


class NodeExit(TypedDict):
    """`node_exit` payload — agent/graph/_node_log.py."""

    node: str
    outcome: str  # ok | cancelled | exception:<Type>
    duration_seconds: float
    exc_name: str | None  # exception outcomes only


class HeartbeatPaused(TypedDict):
    """`heartbeat_paused` payload — ava/self.py."""

    duration_s: float


class HeartbeatNudged(TypedDict):
    """`heartbeat_nudged` payload — services/heartbeat/daemon.py."""

    idle_minutes: int


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


class RecallFilter(TypedDict):
    """`recall_filter` payload — _memory_filter.py; `body` = verdict text."""

    body: str


class ResolvedMarker(TypedDict):
    """`warning_resolved` / `error_resolved` payload.

    Producers name the target with `target_event_id` (exact id) and/or
    `match` (msg-LIKE pattern); the maintenance daemon then writes
    `resolved_by` onto the *target* event's attributes — the read-side
    contract the unresolved-ops panels filter on.
    """

    target_event_id: int
    match: str
    resolved_by: int


class GatewayLatency(TypedDict):
    """`gateway_latency` payload — gateway/_latency.py 60s aggregator.

    One event per (route, 60s bucket) — never per request (Task #1091).
    """

    route: str  # matched route pattern, e.g. /api/agents/{agent_id}/messages
    p50_ms: float
    p95_ms: float
    max_ms: float
    count: int


class LogPayload(TypedDict):
    """`log` payload — bare-log fallback; `msg` rides every loguru-sourced row."""

    msg: str


@dataclass(frozen=True)
class EventSpec:
    """One declared event: name x category x payload x retention x destination.

    ``extra_categories``: a name that genuinely carries more than one category
    (status_change: the loguru side emits telemetry, audit_events emits audit).

    ``destination``: ``"events"`` (default — lands in the ``events`` table) or
    ``"file"`` (log-file only, e.g. ``node_enter`` after PR #1758's sink
    filter). ``family`` groups events the ops panels / rollups treat as one
    family (e.g. LLM_ERROR). ``doc`` is the one-line registry.md description.
    """

    name: str
    category: Category
    extra_categories: frozenset[Category] = frozenset()
    payload: Any | None = None
    retention_days: int | None = None  # None -> RETENTION_BY_CATEGORY[category]
    destination: Literal["events", "file"] = "events"
    family: str | None = None
    doc: str = ""


def _audit(name: str, doc: str, *, payload: Any | None = None) -> EventSpec:
    return EventSpec(name=name, category="audit", payload=payload, doc=doc)


def _telemetry_audit(name: str, doc: str, *, payload: Any | None = None) -> EventSpec:
    """A name that genuinely carries both categories (status_change: the
    loguru side emits telemetry, audit_events emits audit)."""
    return EventSpec(
        name=name,
        category="telemetry",
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
) -> EventSpec:
    return EventSpec(
        name=name,
        category="telemetry",
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
        "MCP tool invoked through the gateway /mcp endpoint (agent_id NULL — external client)",
    ),
    # ── telemetry (category=telemetry) — registry.md §3 ──
    # frontend user modeling
    "frontend_interaction": _telemetry(
        "frontend_interaction",
        "tracked frontend interaction (click / page view / settings change)",
        payload=FrontendInteraction,
    ),
    # turn lifecycle
    "llm_usage": _telemetry("llm_usage", "LLM call metering", payload=LlmUsage),
    "turn_end": _telemetry("turn_end", "one turn finished", payload=TurnEnd),
    "llm_turn_aborted": _telemetry(
        "llm_turn_aborted", "turn aborted after retries", family=LLM_ERROR_FAMILY
    ),
    "compact_turn_aborted": _telemetry(
        "compact_turn_aborted", "turn aborted because compaction failed"
    ),
    "llm_provider_error": _telemetry(
        "llm_provider_error",
        "LLM provider failure",
        payload=LlmProviderError,
        family=LLM_ERROR_FAMILY,
    ),
    "stream_stalled_retry": _telemetry(
        "stream_stalled_retry", "stream stalled, retried", family=LLM_ERROR_FAMILY
    ),
    "stream_overloaded_retry": _telemetry(
        "stream_overloaded_retry", "stream overloaded, retried", family=LLM_ERROR_FAMILY
    ),
    "thinking_block_sanitized": _telemetry("thinking_block_sanitized", "thinking block sanitized"),
    "multiple_tool_calls_merged": _telemetry(
        "multiple_tool_calls_merged", "concurrent tool calls merged"
    ),
    "llm_cancelled": _telemetry("llm_cancelled", "LLM call cancelled"),
    # exec lifecycle
    "exec": _telemetry("exec", "execute_code succeeded", payload=ExecPayload),
    "exec_failed": _telemetry("exec_failed", "execute_code failed", payload=ExecFailed),
    "exec_cancelled": _telemetry("exec_cancelled", "execute_code cancelled"),
    "exec(timeout)": _telemetry(
        "exec(timeout)", "historical parenthesized name (migration target)"
    ),
    "exec(failed)": _telemetry("exec(failed)", "historical parenthesized name (migration target)"),
    "exec(cancelled)": _telemetry(
        "exec(cancelled)", "historical parenthesized name (migration target)"
    ),
    "exec(thread-stuck)": _telemetry(
        "exec(thread-stuck)", "historical parenthesized name (migration target)"
    ),
    "exec_timeout": _telemetry("exec_timeout", "execute_code timed out"),
    "exec_node_timeout": _telemetry("exec_node_timeout", "node-level timeout"),
    "exec_thread_stuck": _telemetry("exec_thread_stuck", "exec thread stuck"),
    "exec_thread_unreapable": _telemetry(
        "exec_thread_unreapable", "orphan exec thread survived the reap window"
    ),
    # hosted runner (future/infra/agent-runner-as-server.md) — the dispatcher
    # that turns an inbound wake into a turn task, and the turn tasks it runs
    "host_dispatcher_subscribed": _telemetry(
        "host_dispatcher_subscribed",
        "hosted dispatcher subscribed to the inbound wake pattern",
    ),
    "host_dispatcher_reconnect": _telemetry(
        "host_dispatcher_reconnect",
        "hosted dispatcher's wake subscription dropped — reconnecting (wakes published "
        "while down are lost; the delivery watchdog re-publish covers them)",
    ),
    "host_dispatcher_bad_channel": _telemetry(
        "host_dispatcher_bad_channel",
        "hosted dispatcher ignored a wake whose channel name carried no agent id",
    ),
    "host_turn_crashed": _telemetry(
        "host_turn_crashed",
        "a hosted turn task raised — the task is dropped and the next wake retries "
        "from the checkpoint; neighbours are unaffected",
    ),
    "host_agent_prepared": _telemetry(
        "host_agent_prepared",
        "the host built an agent's per-agent runtime (chat model + startup reconcile) "
        "on a cold path — carries duration_ms and a reason of cold / config_changed / "
        "evicted, so a wake that pays the cold cost is distinguishable from one that "
        "does not, and a cache thrashing on config churn is visible as reason mix",
    ),
    "host_started": _telemetry(
        "host_started",
        "the hosted agent-runner finished process-scope boot and its dispatcher is live",
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
    ),
    # node / process lifecycle
    "node_enter": _telemetry(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the events table (PR #1758); log files only",
        destination="file",
    ),
    "node_exit": _telemetry("node_exit", "LangGraph node exited", payload=NodeExit),
    "process_exit": _telemetry("process_exit", "agent process exited", payload=ProcessExit),
    "service_started": _telemetry(
        "service_started", "gateway/daemon started", payload=ServiceStarted
    ),
    "halt": _telemetry("halt", "turn stopped (idle/compact/system)", payload=Halt),
    "agent_restarted": _telemetry("agent_restarted", "agent restarted (phase2 done)"),
    "heartbeat_nudged": _telemetry(
        "heartbeat_nudged", "heartbeat reminder", payload=HeartbeatNudged
    ),
    "delivery_stalled": _telemetry("delivery_stalled", "delivery backlog", payload=DeliveryStalled),
    "restart_cas_lost": _telemetry("restart_cas_lost", "restart CAS race lost"),
    "claim_cas_lost": _telemetry(
        "claim_cas_lost", "claim CAS race lost — another lifecycle op owns the row"
    ),
    "claim_cas_lost_exit": _telemetry(
        "claim_cas_lost_exit", "claim wait aborted by a lost CAS — process exiting cleanly"
    ),
    "idle_cas_lost": _telemetry("idle_cas_lost", "idle-flip CAS race lost — degraded, not fatal"),
    "boot_timing": _telemetry("boot_timing", "boot duration"),
    "dangling_tool_use_repaired": _telemetry(
        "dangling_tool_use_repaired", "dangling tool_use repaired"
    ),
    "agent_spawned": _telemetry("agent_spawned", "agent process started", payload=AgentSpawned),
    "agent_resurrected": _telemetry("agent_resurrected", "agent resurrected"),
    "agent_terminated": _telemetry("agent_terminated", "agent terminated"),
    "agent_hibernating": _telemetry("agent_hibernating", "agent hibernated"),
    "agent_swapped_in": _telemetry("agent_swapped_in", "process swapped in"),
    "agent_revived": _telemetry("agent_revived", "agent revived"),
    "respawn_phase1": _telemetry("respawn_phase1", "restart phase 1"),
    "respawn_phase2_launch": _telemetry("respawn_phase2_launch", "restart phase 2 launch"),
    "launch_confirm_extended": _telemetry("launch_confirm_extended", "launch confirm extended"),
    "launch_confirm_failed": _telemetry("launch_confirm_failed", "launch confirm failed"),
    "launch_confirm_task_crashed": _telemetry(
        "launch_confirm_task_crashed", "launch confirm task crashed"
    ),
    "launch_force_terminated": _telemetry("launch_force_terminated", "launch force-terminated"),
    "launch_force_terminated_skipped": _telemetry(
        "launch_force_terminated_skipped", "launch force-terminate skipped"
    ),
    "launch_retry": _telemetry("launch_retry", "launch retried"),
    # sdk / channel health
    "sdk_call": _telemetry("sdk_call", "SDK call metering", payload=SdkCall),
    "plugin_activation": _telemetry(
        "plugin_activation",
        "a plugin injection surface fired (hook / wrap / prompt section)",
        payload=PluginActivation,
    ),
    "sse_drop": _telemetry("sse_drop", "SSE event dropped", payload=SseDrop),
    "event_log_drop": _telemetry("event_log_drop", "event-pipeline row shed", payload=EventLogDrop),
    "heartbeat_paused": _telemetry("heartbeat_paused", "heartbeat paused", payload=HeartbeatPaused),
    "code": _telemetry("code", "LLM generated code block", payload=ExecPayload),
    # label-fallback events kept in the registry (90d retention classification)
    "text": _telemetry("text", "LLM text output"),
    "syntax_fix": _telemetry("syntax_fix", "syntax repair executed", payload=SyntaxFix),
    "inbound_reconcile": _telemetry("inbound_reconcile", "inbound reconciliation"),
    "screen_capture_notify_failed": _telemetry(
        "screen_capture_notify_failed", "screenshot notify failed"
    ),
    # ava.ui.serve page-restore
    "page_restore_alive": _telemetry("page_restore_alive", "page restore alive"),
    "page_restore_reserved": _telemetry("page_restore_reserved", "page restore reserved"),
    "page_restore_query_failed": _telemetry(
        "page_restore_query_failed", "page restore query failed"
    ),
    "page_restore_failed": _telemetry("page_restore_failed", "page restore failed"),
    "page_restore_closed": _telemetry("page_restore_closed", "page restore closed"),
    # db resilience
    "db_outage_wait": _telemetry("db_outage_wait", "db outage wait"),
    "db_outage_pause": _telemetry("db_outage_pause", "db outage pause"),
    "db_outage_reconcile_retry": _telemetry(
        "db_outage_reconcile_retry", "db outage reconcile retry"
    ),
    "db_recovered": _telemetry("db_recovered", "db recovered"),
    "db_pool_acquire_timeout": _telemetry("db_pool_acquire_timeout", "db pool acquire timeout"),
    "db_pool_acquire_slow": _telemetry("db_pool_acquire_slow", "db pool acquire slow"),
    "checkpoint_write_failed": _telemetry("checkpoint_write_failed", "checkpoint write failed"),
    "pgbouncer_repaired": _telemetry("pgbouncer_repaired", "pgbouncer watchdog repair"),
    # labeler / trace housekeeping
    "label_generated": _telemetry("label_generated", "label auto-generated"),
    "label_generate_failed": _telemetry("label_generate_failed", "label generation failed"),
    "label_generate_skipped": _telemetry("label_generate_skipped", "label generation skipped"),
    "label_generate_empty": _telemetry("label_generate_empty", "label generation empty"),
    "label_generate_rejected": _telemetry(
        "label_generate_rejected", "label generation rejected as not a label"
    ),
    "label_generate_retired": _telemetry(
        "label_generate_retired", "label generation given up on after repeated failures"
    ),
    "trace": _telemetry("trace", "otel span export"),
    # agent lifecycle / state
    "idle_wake": _telemetry("idle_wake", "agent woken from idle", payload=IdleWake),
    # compact / checkpoint / memory housekeeping
    "compact_request": _telemetry("compact_request", "compact requested"),
    "auto_compact": _telemetry("auto_compact", "auto-compact"),
    "compact_reminder": _telemetry("compact_reminder", "compact reminder"),
    "history_dump": _telemetry("history_dump", "pre-compact history dumped to workspace"),
    "checkpoint_trim": _telemetry("checkpoint_trim", "checkpoint trimmed"),
    "recall_filter": _telemetry("recall_filter", "memory recall filter", payload=RecallFilter),
    "passive_recall": _telemetry("passive_recall", "passive memory recall"),
    "silent_idle": _telemetry("silent_idle", "silent idle verdict"),
    "last_msg": _telemetry("last_msg", "last-message check"),
    # gateway endpoint latency metering (Task #1091): 60s aggregates emitted
    # by gateway/_latency.py — one event per (route, bucket), never per request
    "gateway_latency": _telemetry(
        "gateway_latency",
        "gateway endpoint latency — 60s aggregate per route (p50/p95/max/count)",
        payload=GatewayLatency,
    ),
    # ── log (category=log) — registry.md §4, the bare-log fallback ──
    "log": EventSpec(name="log", category="log", payload=LogPayload, doc="bare log line"),
    # unresolved-ops markers: a warning/error (or a class of them) declared fixed —
    # ops panels filter these out of the "unresolved" views (user ruling 2026-08-09)
    "warning_resolved": EventSpec(
        name="warning_resolved",
        category="log",
        payload=ResolvedMarker,
        doc="mark a warning (or class of warnings, via attributes.match) resolved",
    ),
    "error_resolved": EventSpec(
        name="error_resolved",
        category="log",
        payload=ResolvedMarker,
        doc="mark an error (or class of errors, via attributes.match) resolved",
    ),
}


# ── derived views — the only spellings consumers may use ───────────────────


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
