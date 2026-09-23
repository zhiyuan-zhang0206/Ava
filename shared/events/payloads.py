"""Event vocabulary and runtime payload schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

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
    task-associated system note; untagged calls do not belong to a task.

    ``cache_mechanism`` / ``cache_scope`` are present only when the call site
    knows the request's cache provenance (task #2660): the Gemini explicit
    cache path labels ``mixed`` / ``explicit_block`` because the API reports
    only the explicit block in ``cache_read``; absent keys mean unknown, never
    fabricated."""

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
    cache_mechanism: NotRequired[str]
    cache_scope: NotRequired[str]


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


class StreamStalledRetry(TypedDict):
    """`stream_stalled_retry` payload — agent/graph/_llm_stream.py.

    The stalled stream's provider identity and shape, so stalls are countable
    per vendor/model — the provider-health dimension the LLM telemetry
    previously lacked (2026-09-14/15 wave: 100% of stalled requests were
    api.deepseek.com, but nothing in the event stream said so).

    ``elapsed_s`` is the stalled segment's wall-clock (the bound that expired);
    it also maps onto the OTLP metric surface as a histogram with
    ``vendor``/``model``/``stage`` as datapoint attributes. ``stage`` is
    ``ttft`` (no first chunk), ``mid-stream`` (gap after chunks arrived) or
    ``total`` (the per-attempt duration ceiling).
    """

    vendor: str | None
    model: str
    stage: str
    elapsed_s: float


class StreamStallPairTerminated(TypedDict):
    """`stream_stall_pair_terminated` payload — agent/graph/_llm_stream.py.

    The call-terminating stall pair (the stream segment and its non-streaming
    fallback both expired) carries the same provider identity as the
    ``stream_stalled_retry`` it co-emits with, so the pair joins back to the
    stall that opened it; ``timeout_s`` is the shared
    ``llm_stream_ttft_timeout_seconds`` bound both segments ran under.
    """

    vendor: str | None
    model: str
    stage: str
    timeout_s: float


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


class ExecRequestQuarantine(TypedDict):
    """`exec_request_quarantine` payload — stale exec request evidence preserved."""

    reason: str
    event_dir: str
    sources: list[str]
    vanished: list[str]


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
    """Actual queue loss; timestamp drives the cluster error-state window."""

    n: int
    queue: NotRequired[str]
    last_dropped_at: NotRequired[float]


class SdkCall(TypedDict):
    """`sdk_call` payload — ava/_sdk_metering.py recorder."""

    fn: str
    duration: float
    sample_rate: int
    detail: NotRequired[dict[str, Any]]


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


class PauseLifecycleWait(TypedDict):
    """`pause_lifecycle_wait` payload — ops/agent_pause.py::_prepare.

    One row per preparation episode that met in-flight work it did not author
    (task #3591). ``waited_s`` is the bounded retry time before the outcome:
    ``resolved`` (the work finished and preparation proceeded), ``exceeded``
    (the bound was spent — abort), or ``refused`` (maintenance-authored work —
    no wait by design).
    """

    waited_s: float
    outcome: Literal["resolved", "exceeded", "refused"]
    agents: list[int]


class UpdateStragglerReaped(TypedDict):
    """`update_straggler_reaped` payload — ops/agent_pause.py::_drain.

    One row per drain pass that reaped stragglers (task #4016): the cohort
    members CAS-marked 'restarting' past the configured restart window, and
    the window in force. The durable trail is the hold journal's `reaped`
    receipts plus each member's mark (settled at the successor boot/resume).
    """

    agents: list[int]
    window_s: float


class UpdateStragglerReapSettled(TypedDict):
    """`update_straggler_reap_settled` payload — shared/straggler_reap.py.

    One row per boot/resume boundary that restored stranded reap marks
    (task #4016). `site` is "boot" (the agent-host boot) or "resume" (the
    local unpause/start path).
    """

    agents: list[int]
    site: str


class ShellTtlRenewed(TypedDict):
    """`shell_ttl_renewed` payload — ava/shell/sessions.py renew().

    One event per explicit deadline extension: the requested ttl and the
    before/after deadlines (DB clock, ISO strings). The durable trail is the
    `agent_shell_ttl_renewals` audit table; this event is the display
    surface.
    """

    session_id: int
    ttl_s: float
    prev_expires_at: str
    new_expires_at: str


class HeartbeatNudged(TypedDict):
    """`heartbeat_nudged` payload — services/heartbeat/daemon.py."""

    idle_minutes: int


class HeartbeatBackoffRaised(TypedDict):
    """`heartbeat_backoff_raised` payload — services/heartbeat/daemon.py.

    Emitted when N consecutive no-op nudges raise an agent's platform-side
    nudge-backoff level (B7).
    """

    level: int
    interval_seconds: int


class HeartbeatBackoffReset(TypedDict):
    """`heartbeat_backoff_reset` payload — services/heartbeat/daemon.py.

    Emitted when a real inbound or an agent pause resets the level to 0 (B7).
    """

    previous_level: int
    reason: str


class CiUsageDaily(TypedDict):
    """`ci_usage_daily` payload — schedules/c9-daily-report-schedule.py.

    One event per reconciliation day (05:00-05:00 cluster time): the CI
    workflow's runs and ceil-billed minutes for the window, split by
    attribution (PR-title [Ava-<id>] convention) and OS. `est_usd` is the
    private-repo overage equivalent at the GitHub-hosted rates — the repo is
    currently public, so minutes are the billing fact (scripts/ci_accounting.py).
    Per-agent detail lives in the attribution ledger, not here.
    """

    day: str  # cluster-tz date of the window end
    window_start: str  # UTC ISO
    window_end: str  # UTC ISO
    runs: int
    attributed_runs: int
    unattributed_runs: int
    total_minutes: int
    attributed_minutes: int
    linux_minutes: int
    macos_minutes: int
    appended_runs: int  # new ledger rows this reconciliation (0 = re-fire no-op)
    est_usd: float


class DebtSweepDaily(TypedDict):
    """`debt_sweep_daily` payload — schedules/debt-sweep-daily-schedule.py.

    One event per claimed 06:30 cluster-time slot. The mechanical scan may
    fail without blocking the clearing worker, so scan status is an explicit
    fact rather than an implied success from the worker action.
    """

    day: str  # cluster-tz date of the claimed slot
    scan_status: str  # ok | failed
    action: str  # spawned | resurrected | messaged
    worker_agent_id: int


class PrFlowDaily(TypedDict):
    """`pr_flow_daily` payload — scripts/pr_flow_export.py (macmini daily job).

    One event per complete cluster-tz day in the trailing window, re-emitted
    on every run so the whole window stays inside Prometheus's retention.
    Every numeric field is absolute per-day state, never a sum, and the OTLP
    disposition records each as an ObservableGauge
    (``shared/telemetry/otlp/telemetry_otlp.py``) — a counter or histogram would accrue
    across re-emissions. Fields are absent when the day has no such sample
    (no merges -> no percentile/round values; an unreachable flaky source
    omits ``flake_new_quarantines`` rather than claiming zero).
    """

    day: str  # cluster-tz date (Asia/Shanghai fleet clock)
    merged_count: int  # PRs whose merged_at falls on the day
    ready_to_merge_median_seconds: NotRequired[float]
    ready_to_merge_p90_seconds: NotRequired[float]
    qa_rounds_mean: NotRequired[float]  # ava-qa receipts per merged PR
    qa_rereview_share: NotRequired[float]  # share with a post-receipt head change
    flake_new_quarantines: NotRequired[int]  # tests quarantined on the day


class PrFlowRun(TypedDict):
    """`pr_flow_run` payload — scripts/pr_flow_export.py (macmini daily job).

    One event per run: the point-in-time Trunk queue depth sample. Absolute
    state -> ObservableGauge (``ava_pr_flow_run_queue_depth_ratio``). The
    event is emitted even when the depth sample is unavailable, so the daily
    breadcrumb survives a Trunk outage; the missing field stays absent (an
    absent optional metric is not zero).
    """

    queue_depth: NotRequired[int]


class CiRunsDaily(TypedDict):
    """`ci_runs_daily` payload — scripts/ci_runs_export.py.

    One absolute-state sample per complete cluster-time day and repository.
    The collector re-emits its trailing window, so every number is an OTLP
    gauge rather than an accumulating counter. Classification labels overlap;
    `white_run_share` alone deduplicates instant skips, superseded runs, and
    abandoned PR heads in that priority order. Per-PR percentiles are absent
    when no completed PR has attributed runs. `first_pass_pr_share` is v1's
    run-level approximation: attributed non-noise, non-zombie runs have no
    red conclusion and no retry attempt.
    """

    repo: str
    day: str
    runs: int
    instant_skip_runs: int
    watchdog_runs: int
    qa_gate_runs: int
    proof_runs: int
    cancelled_runs: int
    superseded_runs: int
    superseded_zero_runs: int
    failed_runs: int
    retried_failed_runs: int
    self_healed_runs: int
    abandoned_runs: int
    zombie_runs: int
    prs_completed: int
    prs_with_runs: int
    per_pr_duration_median_minutes: NotRequired[float]
    per_pr_duration_p90_minutes: NotRequired[float]
    per_pr_runs_median: NotRequired[float]
    per_pr_runs_p90: NotRequired[float]
    per_pr_runs_executed_median: NotRequired[float]
    white_run_share: NotRequired[float]
    retry_share: NotRequired[float]
    noise_run_share: NotRequired[float]
    first_pass_pr_share: NotRequired[float]


class CiWorkflowWindow(TypedDict):
    """`ci_workflow_window` payload — scripts/ci_runs_export.py.

    Trailing-window absolute workflow state, keyed by repository and workflow
    name, emitted with the same daily sampler. Execution percentiles omit
    zombie wall-clock artifacts and zero-execution runs rather than treating
    an empty population as a real zero.
    """

    repo: str
    workflow: str
    runs: int
    failed_runs: int
    self_healed_runs: int
    retried_failed_runs: int
    cancelled_runs: int
    superseded_runs: int
    instant_skip_runs: int
    prs_appeared_on: int
    pr_appearance_share: NotRequired[float]
    retry_share: NotRequired[float]
    exec_median_seconds: NotRequired[float]
    exec_p90_seconds: NotRequired[float]


class CiRunsRun(TypedDict):
    """`ci_runs_run` payload — scripts/ci_runs_export.py.

    One sampler breadcrumb per repository: the fixed window's population and
    this run's GitHub-read budget. These are current observations, so all
    numbers are gauges even though their names are counts.
    """

    repo: str
    window_days: int
    window_runs: int
    window_prs: int
    api_requests: int


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


class LokiWritePathProbeThrottled(TypedDict):
    """`loki_write_path_probe_throttled` payload — LGTM write-path healthcheck."""

    consecutive_throttles: int
    reason: str


class DeliveryPoisoned(TypedDict):
    """`delivery_poisoned` payload — delivery watchdog dispatch guard."""

    inbound_id: int
    dispatch_count: int
    age_s: float


class DeliveryWakeSuppressed(TypedDict):
    """`delivery_wake_suppressed` payload — delivery watchdog resurrection guard."""

    consecutive_failures: int
    suppress_seconds: float
    suppress_count: int
    reason: str


class DeliveryRecoveryDecision(TypedDict):
    """`delivery_recovery_decision` payload — services/delivery_watchdog/daemon.py.

    One decision the delivery watchdog obtained for a stalled chat whose owner
    is a crash-marked idling corpse (task #3618): `decision` is the home
    runner's verdict ('harvested' / 'already_terminated' / 'refused') or a
    local transport outcome ('unreachable' / 'error'); `reason` carries the
    fail-closed cause of a refusal, else None."""

    inbound_id: int
    decision: str
    reason: str | None


class DeliveryOutboxFlushed(TypedDict):
    """`delivery_outbox_flushed` payload — shared/delivery_outbox.py flusher."""

    inbound_id: int
    attempts: int
    flush_attempts: int
    age_s: float
    origin_agent_id: int | None


class DeliveryOutboxAbandoned(TypedDict):
    """`delivery_outbox_abandoned` payload — shared/delivery_outbox.py flusher.

    `reason` stays the stable code readers match on; `detail` carries the
    readable failure text when the abandonment had one (gate refusal, key
    conflict, last transport error).
    """

    reason: str
    detail: str | None
    attempts: int
    flush_attempts: int
    age_s: float
    origin_agent_id: int | None


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
