"""Telemetry backends, query budgets, event-class resolution and audit-record events."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class EventLogDrop(TypedDict):
    """Actual queue loss; timestamp drives the cluster error-state window."""

    n: int
    queue: NotRequired[str]
    last_dropped_at: NotRequired[float]


class LokiWritePathProbeFailed(TypedDict):
    """`loki_write_path_probe_failed` payload — LGTM write-path healthcheck."""

    consecutive_failures: int
    reason: str


class LokiWritePathProbeThrottled(TypedDict):
    """`loki_write_path_probe_throttled` payload — LGTM write-path healthcheck."""

    consecutive_throttles: int
    reason: str


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
    process: NotRequired[str]
    agent_id: NotRequired[int | None]
    dismissed_by: NotRequired[int]
    note: NotRequired[str]


class EventClassReopened(TypedDict):
    """`warning_reopened` / `error_reopened` immutable class-state marker."""

    category: str
    level: str
    event_name: str
    source: str
    process: str
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


class TelemetryReadStale(TypedDict):
    """`telemetry_read_stale` payload — gateway/lgtm/telemetry_staleness.py."""

    source: str
    signal: str
    threshold_s: int
    age_s: float | None
    action: str
    reason: str


class TelemetryReadRecovered(TypedDict):
    """`telemetry_read_recovered` payload — gateway/lgtm/telemetry_staleness.py."""

    source: str
    signal: str
    stale_duration_s: float


class OtlpBackendDisabled(TypedDict):
    """`otlp_backend_disabled` payload — base/telemetry/otlp/telemetry_otlp.py."""

    reason: str
    endpoint: str | None


class OtlpBackendRecovered(TypedDict):
    """`otlp_backend_recovered` payload — base/telemetry/otlp/telemetry_otlp.py."""

    endpoint: str | None
    disabled_s: float | None


class LokiQueryFailed(TypedDict):
    """`loki_query_failed` payload — gateway/lgtm/loki_events.py transport failure.

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
    """`prom_query_failed` payload — gateway/lgtm/prom_metrics.py transport failure."""

    endpoint: str
    duration_s: float
    error: str
    query: str


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


class AuditWriteFailed(TypedDict):
    """`audit_write_failed` payload — base/telemetry/audit_events.py.

    An audit event could not be recorded in `audit_events`, from a producer that
    must not fail its caller (an agent-facing tool call that already succeeded,
    or a state transition whose remaining steps must still run). The record is
    missing; the Loki projection of the same event still went out. ``event_name``
    is the audit event that was lost, ``error`` a truncated message.
    """

    event_name: str
    error_class: str
    error: str


class LogPayload(TypedDict):
    """`log` payload — bare-log fallback; `msg` rides every loguru-sourced row."""

    msg: str


EVENTS: dict[str, EventSpec] = {
    "loki_write_path_probe_failed": telemetry_event(
        "loki_write_path_probe_failed",
        "Loki write-path probe failed",
        payload=LokiWritePathProbeFailed,
        tier="anomaly",
        site="services/healthchecks/lgtm.py write-path probe",
    ),
    "loki_write_path_probe_throttled": telemetry_event(
        "loki_write_path_probe_throttled",
        "Loki write-path probe persistently throttled",
        payload=LokiWritePathProbeThrottled,
        tier="anomaly",
    ),
    "event_log_drop": telemetry_event(
        "event_log_drop",
        "event-pipeline row shed",
        payload=EventLogDrop,
        tier="anomaly",
        site="base/telemetry/loss.py:loss_event constructs Event directly",
    ),
    "telemetry_read_stale": telemetry_event(
        "telemetry_read_stale",
        "read-side telemetry staleness detected — heartbeat older than threshold",
        payload=TelemetryReadStale,
        tier="anomaly",
        site="gateway/lgtm/telemetry_staleness.py:_emit",
    ),
    "telemetry_read_recovered": telemetry_event(
        "telemetry_read_recovered",
        "read-side telemetry heartbeat recovered",
        payload=TelemetryReadRecovered,
        site="gateway/lgtm/telemetry_staleness.py:_emit",
    ),
    "otlp_backend_disabled": telemetry_event(
        "otlp_backend_disabled",
        "OTLP backend disabled for this process (init failure / collector unreachable); retry scheduled",
        payload=OtlpBackendDisabled,
        tier="anomaly",
        site="base/telemetry/otlp/telemetry_otlp.py:_emit_backend_event",
    ),
    "otlp_backend_recovered": telemetry_event(
        "otlp_backend_recovered",
        "OTLP backend brought up after a disabled episode (periodic retry)",
        payload=OtlpBackendRecovered,
        site="base/telemetry/otlp/telemetry_otlp.py:_emit_backend_event",
    ),
    "loki_query_budget": telemetry_event(
        "loki_query_budget",
        "local Loki query-admission transition and capacity metrics",
        payload=LokiQueryBudget,
        tier="noise",
        site="gateway/lgtm/loki_query_budget.py:_emit_observation",
    ),
    "prom_query_budget": telemetry_event(
        "prom_query_budget",
        "local Prometheus query-admission transition and capacity metrics",
        payload=PromQueryBudget,
        tier="noise",
        site="gateway/lgtm/prom_metrics.py:_emit_budget_observation",
    ),
    # Immutable Loki lines cannot be updated with a `resolved_by` attribute.
    # These markers record class-state transitions while `event_dismissals`
    # remains the active-resolution source of truth (task #1468).
    "warning_resolved": telemetry_event(
        "warning_resolved",
        "class-level warning dismissal marker (legacy target-event attributes remain accepted)",
        payload=ResolvedMarker,
        tier="anomaly",
        site=(
            "Class-resolution markers select their name from the event level at "
            "runtime; services/events_maintenance/resolution.py emits the reopen "
            "markers and resolution_status, while the gateway emits resolved ones."
        ),
    ),
    "error_resolved": telemetry_event(
        "error_resolved",
        "class-level error/critical dismissal marker (legacy target-event attributes remain accepted)",
        payload=ResolvedMarker,
        tier="anomaly",
        site=(
            "Class-resolution markers select their name from the event level at "
            "runtime; services/events_maintenance/resolution.py emits the reopen "
            "markers and resolution_status, while the gateway emits resolved ones."
        ),
    ),
    "warning_reopened": telemetry_event(
        "warning_reopened",
        "class-level warning dismissal reopened manually or by the burst safety valve",
        payload=EventClassReopened,
        tier="anomaly",
        site=(
            "Class-resolution markers select their name from the event level at "
            "runtime; services/events_maintenance/resolution.py emits the reopen "
            "markers and resolution_status, while the gateway emits resolved ones."
        ),
    ),
    "error_reopened": telemetry_event(
        "error_reopened",
        "class-level error/critical dismissal reopened manually or by the burst safety valve",
        payload=EventClassReopened,
        tier="anomaly",
        site=(
            "Class-resolution markers select their name from the event level at "
            "runtime; services/events_maintenance/resolution.py emits the reopen "
            "markers and resolution_status, while the gateway emits resolved ones."
        ),
    ),
    "resolution_status": telemetry_event(
        "resolution_status",
        "absolute unresolved + dismissed warning/error class counts over the daemon's fixed six-hour window",
        payload=ResolutionStatus,
        tier="noise",
        site=(
            "Class-resolution markers select their name from the event level at "
            "runtime; services/events_maintenance/resolution.py emits the reopen "
            "markers and resolution_status, while the gateway emits resolved ones."
        ),
    ),
    "checkpoint_table_sizes": telemetry_event(
        "checkpoint_table_sizes",
        "checkpoint table physical sizes and live row counts (hourly + after each blob vacuum run)",
        payload=CheckpointTableSizes,
        site="services/events_maintenance/blob_vacuum.py telemetry.emit (positional)",
    ),
    "audit_write_failed": telemetry_event(
        "audit_write_failed",
        "an audit event could not be recorded in audit_events (the record is missing; "
        "the Loki projection of the same event still went out)",
        payload=AuditWriteFailed,
        tier="anomaly",
        site=('base/telemetry/audit_events.py:_report_unrecorded telemetry.emit("telemetry", ...)'),
    ),
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
}
