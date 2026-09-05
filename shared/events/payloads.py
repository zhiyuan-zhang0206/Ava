"""Event vocabulary and runtime payload schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, NotRequired, TypedDict

Category = Literal["audit", "telemetry", "log"]
EventTier = Literal["business", "anomaly", "observation", "noise"]
RetentionClass = Literal["lineage", "audit", "lifecycle", "telemetry", "log"]

# Retention class is the third, independent dimension (design 2026-09-02,
# user ruling): `category` decides access semantics, `tier` decides display
# priority, and neither can say "this row must never be deleted" — lineage is
# 5 of the 17 audit names. It answers one question: after this row is gone,
# can the fact still be reconstructed?
#
# - lineage: no. Who spawned whom is not derivable from any current state
#   (`agents_meta.spawner` is folded on terminate), and the class is tiny
#   (~412 rows/day, 0.11% of the stream), so it is retained permanently and
#   append-only — a 100-year Loki per-stream period plus its own JSONL mirror.
# - audit / lifecycle / telemetry / log: reconstructable, approximable, or
#   aggregated. Their windows are not declared here yet (this change ships the
#   lineage class only); the names exist so the vocabulary is fixed and a later
#   declaration is one field, not a new dimension.
#
# Declaring a class here is half a change: the deployed Loki `retention_stream`
# rule is derived from `lineage_event_names()` and pinned by
# `shared/loki_index_labels.validate_loki_deploy_config`, because the
# 2026-08-20 archive loss shipped as exactly that half — the per-stream
# override landed nine days after the global 168h bucket had deleted the data.

# Event tiers control the human-facing event stream, independently from the
# category that controls event-class access semantics:
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
    call volume is countable in Prometheus); it is omitted on priced calls.
    ``task_id`` is present only when the turn was explicitly driven by a
    task-associated system note; untagged calls do not belong to a task."""

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
    task_id: NotRequired[int]
    usage_kind: str
    source: NotRequired[str]


class TurnEnd(TypedDict):
    """`turn_end` payload — agent/graph/_llm.py."""

    ok: bool
    duration_seconds: float


class SilentIdle(TypedDict):
    """`silent_idle` payload — output-token cost-boundary verdict."""

    output_tokens: int
    cumulative_output_tokens: int
    estimated_cost_usd: float | None
    halted: bool


class LlmRetry(TypedDict):
    """`llm_retry` payload — final duration of a retry sequence."""

    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"]
    duration_seconds: float


class LlmProviderError(TypedDict):
    """`llm_provider_error` payload — shared/lm/errors.py.

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


class ExecChildBoot(TypedDict):
    """`exec_child_boot` payload — child bootstrap duration before agent code."""

    duration_ms: float


class CompactionCompleted(TypedDict):
    """`compaction_completed` payload — one applied history replacement."""

    compact_kind: str
    compactions: int
    history_chars: int
    summary_chars: int
    summary_history_ratio: float | None


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
    wake_state: str


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


class LokiWritePathProbeFailed(TypedDict):
    """`loki_write_path_probe_failed` payload — LGTM write-path healthcheck."""

    consecutive_failures: int
    reason: str


class DeliveryPoisoned(TypedDict):
    """`delivery_poisoned` payload — delivery watchdog dispatch guard."""

    inbound_id: int
    dispatch_count: int
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
