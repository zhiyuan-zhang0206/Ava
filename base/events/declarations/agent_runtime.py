"""In-agent runtime events: SDK, plugins, hooks, recall, heartbeat, history, labels and db resilience."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class SdkCall(TypedDict):
    """`sdk_call` payload — ava/sdk_surface/metering.py recorder."""

    fn: str
    duration: float
    sample_rate: int
    detail: NotRequired[dict[str, Any]]


class PluginActivation(TypedDict):
    """`plugin_activation` payload — base/packages/plugins/activation.py.

    ``plugin`` / ``surface`` / ``identifier`` are the same triple
    ``base.packages.plugins.contributions.Contribution`` stores, so the declared
    contributions and these runtime records join on three strings. ``detail`` is free
    text about the one firing; ``model`` is the model in force, which is what
    makes philosophy §6's per-model obsolescence gauge answerable."""

    plugin: str
    surface: str
    identifier: str
    detail: str
    model: str


class HeartbeatPaused(TypedDict):
    """`heartbeat_paused` payload — ava/self.py."""

    duration_s: float


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
    """`passive_recall` payload — memory_recall.py / ava_memory plugin.

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


class DeltaMessageSuffix(TypedDict):
    """Message-write transfer totals, including the final batch's unused prefix.

    The optional snapshot seed is excluded; the enclosing reconstruction span
    counts it together with every fetched write body.
    """

    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    candidate_writes: int
    fetched_rows: int
    fetched_bytes: int
    body_batches: int
    retained_writes: int
    reset_found: bool


class PluginLoadFailed(TypedDict):
    """`plugin_load_failed` payload — base/packages/plugins/load_report.py.

    One row per plugin that could not be loaded at a plugin-code load site
    reporting through this canonical reporter: `plugin.py` at host boot or
    graph build, `provider.py`, `services.py`, `setup.py`, a built-in
    plugin's `metrics.py`, the gateway plugin inspector's `inspector.py`, a
    dangling config entry. Two contained sites stay off this reporter (not
    oversights): `default_config.py` images surface as `error` entries on the
    plugin-update result, and the runtime config readers' dangling warning is
    a plain log line — only the graph loader reports dangling entries as rows
    here. The plugin is skipped — fail-soft contract (user ruling 2026-09-11,
    after the 2026-08-28 ava_ledger and 2026-09-10 agent-host incidents): a
    broken plugin must never block `import ava` / host boot / graph build for
    the whole cluster. This event is the loud half of that contract; `error`
    carries the exception type + message so ops sees which plugin broke and
    why.
    """

    plugin: str
    error: str


EVENTS: dict[str, EventSpec] = {
    "plugin_load_failed": telemetry_event(
        "plugin_load_failed",
        "enabled plugin skipped because it failed to load (fail-soft)",
        payload=PluginLoadFailed,
        tier="anomaly",
        site=(
            "base/packages/plugins/load_report.py:report_plugin_load_failure "
            'telemetry.emit("telemetry", ...)'
        ),
    ),
    "heartbeat_nudged": telemetry_event(
        "heartbeat_nudged",
        "heartbeat reminder",
        payload=HeartbeatNudged,
        tier="noise",
        site="services/heartbeat/daemon.py:_alert_idle",
    ),
    "heartbeat_backoff_raised": telemetry_event(
        "heartbeat_backoff_raised",
        "no-op nudge backoff level raised",
        payload=HeartbeatBackoffRaised,
        tier="noise",
        site="services/heartbeat/daemon.py:_raise_backoff_level (positional emit)",
    ),
    "heartbeat_backoff_reset": telemetry_event(
        "heartbeat_backoff_reset",
        "nudge backoff reset by real inbound or pause",
        payload=HeartbeatBackoffReset,
        tier="noise",
        site="services/heartbeat/daemon.py:_sweep_backoff_resets (positional emit)",
    ),
    "dangling_tool_pairing_repaired": telemetry_event(
        "dangling_tool_pairing_repaired", "dangling tool pairing repaired", tier="anomaly"
    ),
    "delta_read_compat": telemetry_event(
        "delta_read_compat",
        "delta-written checkpoint messages reconstructed for a plain reader "
        "(task #3180 transition layer)",
        tier="noise",
    ),
    # sdk / channel health
    "sdk_call": telemetry_event(
        "sdk_call",
        "SDK call metering",
        payload=SdkCall,
        tier="noise",
        site="ava/sdk_surface/metering.py recorder (via base/sdk_telemetry)",
    ),
    "plugin_activation": telemetry_event(
        "plugin_activation",
        "a plugin injection surface fired (hook / wrap / prompt section)",
        payload=PluginActivation,
        tier="noise",
        site=(
            "base/packages/plugins/activation.py:emit binds "
            "event=PLUGIN_ACTIVATION_EVENT (a module constant, like sdk_call), so "
            "the literal scan cannot see it."
        ),
        persist=True,
    ),
    "heartbeat_paused": telemetry_event(
        "heartbeat_paused",
        "heartbeat paused",
        payload=HeartbeatPaused,
        site='ava/self.py:258 telemetry.emit("telemetry", ...)',
        persist=True,
    ),
    "shell_ttl_renewed": telemetry_event(
        "shell_ttl_renewed",
        "shell TTL deadline renewed",
        payload=ShellTtlRenewed,
        site='ava/shell/sessions.py:_record_renewal telemetry.emit("telemetry", ...)',
    ),
    "screen_capture_notify_failed": telemetry_event(
        "screen_capture_notify_failed", "screenshot notify failed", tier="anomaly"
    ),
    "delta_message_suffix": telemetry_event(
        "delta_message_suffix",
        "message history write-body transfer and retained suffix counts; excludes snapshot seed",
        payload=DeltaMessageSuffix,
        tier="noise",
    ),
    # db resilience
    "db_outage_wait": telemetry_event(
        "db_outage_wait", "db outage wait", tier="anomaly", retired=True
    ),
    "db_outage_pause": telemetry_event(
        "db_outage_pause", "db outage pause", tier="anomaly", retired=True
    ),
    "db_outage_reconcile_retry": telemetry_event(
        "db_outage_reconcile_retry",
        "db outage reconcile retry",
        tier="anomaly",
        retired=True,
    ),
    "db_recovered": telemetry_event("db_recovered", "db recovered", tier="anomaly", retired=True),
    "db_pool_acquire_timeout": telemetry_event(
        "db_pool_acquire_timeout", "db pool acquire timeout", tier="anomaly"
    ),
    "db_pool_acquire_slow": telemetry_event(
        "db_pool_acquire_slow", "db pool acquire slow", tier="anomaly"
    ),
    "checkpoint_write_failed": telemetry_event(
        "checkpoint_write_failed", "checkpoint write failed", tier="anomaly"
    ),
    "trace": telemetry_event("trace", "otel span export", tier="noise"),
    "history_dump": telemetry_event(
        "history_dump", "pre-compact history dumped to workspace", tier="noise"
    ),
    "checkpoint_trim": telemetry_event("checkpoint_trim", "checkpoint trimmed", tier="noise"),
    "recall_filter": telemetry_event(
        "recall_filter",
        "memory recall filter",
        payload=RecallFilter,
        tier="noise",
        persist=True,
    ),
    "passive_recall": telemetry_event(
        "passive_recall",
        "passive memory recall",
        payload=PassiveRecall,
        tier="noise",
        persist=True,
    ),
    "hook_timing": telemetry_event(
        "hook_timing",
        "hook-runner pass — per-hook wall durations, attributing a slow before_llm / "
        "before_exec node to its hooks from events alone",
        payload=HookTiming,
        tier="noise",
    ),
    "last_msg": telemetry_event("last_msg", "last-message check", tier="noise"),
}
