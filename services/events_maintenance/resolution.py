"""Immutable event-class resolution — Loki counts -> dismissal state -> gauges.

The event stream is append-only in Loki, so no resolution path may write a
``resolved_by`` attribute back onto historical events. Active rows in
``event_dismissals`` instead remove one exact (category, level, event_name,
source) class from the count. The companion ten-minute query is the safety
valve: a renewed burst reopens the class before it can hide a new incident.

The class arithmetic is window-agnostic: :func:`level_splits` turns any
window's per-class counts plus the active dismissals into per-level
total / dismissed / net triples. The daemon's :func:`run_resolution_slice`
applies it to its fixed six-hour window and publishes the unresolved and
dismissed gauges; the gateway stats dashboard (``gateway/routers/status.py``)
applies the same arithmetic to the frontend-selected window, so the two
surfaces agree class for class.

The public seams are :func:`run_resolution_slice`, :func:`level_splits`,
:func:`grouped_count_query`, and :func:`active_dismissals`; tests replace
``_query_class_counts`` to cover the arithmetic without a Loki process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from services.events_maintenance.rollup import _query_instant
from shared import telemetry
from shared.config import settings
from shared.loki_index_labels import escape_logql_label

_log = logging.getLogger("services.events_maintenance.resolution")

_UNRESOLVED_WINDOW = "6h"
_BURST_WINDOW = "10m"
_AUTO_SLICE = timedelta(hours=6)


@dataclass(frozen=True)
class EventClass:
    """The immutable-event resolution identity (per-agent scope is reserved)."""

    category: str
    level: str
    event_name: str
    source: str
    agent_id: int | None = None


@dataclass(frozen=True)
class Dismissal:
    """One active dismissal row used by the resolution arithmetic."""

    id: int
    event_class: EventClass
    dismissed_by: int
    note: str


@dataclass(frozen=True)
class ResolutionResult:
    """One successful resolution pass, useful for logging and direct tests."""

    unresolved_warnings: int
    unresolved_errors: int
    reopened: int
    auto_dismissed: int


@dataclass(frozen=True)
class LevelSplit:
    """The three-way breakdown of one level within a window.

    ``dismissed`` counts the window events whose class has an active
    dismissal; ``net`` is what remains after subtracting them. ``total`` is
    always ``dismissed + net`` — the dashboard and the Grafana gauges derive
    all three from the same class counts, so the user-visible trio stays
    consistent by construction.
    """

    total: int
    dismissed: int
    net: int


_last_auto_dismiss_day: list[date | None] = [None]


def grouped_count_query(window: str, *, cluster: str | None = None) -> str:
    """One capped series aggregation for the event classes in ``window``.

    Do not add category/level/event-name stream-selector labels before
    ``LEGACY_READ_EXPIRES_AT = 2026-08-30T11:10Z``. Legacy chunks have those
    values only in their JSON body; filtering them in the selector would make
    active dismissals depend on a rollout boundary (#1467).

    ``cluster`` is an optional pipeline stage (``| cluster="X" or cluster=""``,
    the same unlabeled-row acceptance the gateway's aggregate pipelines use)
    — the daemon's fixed-window gauge pass leaves it unset, while the gateway
    dashboard scopes its raw counts to the current home cluster. The
    dismissal SET stays global either way, so the two surfaces cancel the
    same classes (task #1935).
    """

    pipeline = (
        '{service_name="unknown_service"} | json | '
        'category=~"telemetry|log" | level=~"warning|error|critical"'
    )
    if cluster is not None:
        escaped = escape_logql_label(cluster)
        pipeline += f' | cluster="{escaped}" or cluster=""'
    return f"sum by (category, level, event_name, source) (count_over_time({pipeline} [{window}]))"


def _query_class_counts(window: str, at: datetime) -> dict[EventClass, int]:
    """Read one grouped Loki vector as exact event-class counts.

    ``sum by`` gives one series per class via :func:`grouped_count_query` —
    the same LogQL the gateway dashboard runs over its selected window, so a
    class dismissed on one surface is subtracted identically on the other. A
    malformed group is a read failure rather than a guessed class: emitting a
    stale zero would make an unhealthy Loki side look resolved.
    """

    rows = _query_instant(grouped_count_query(window), at)
    counts: dict[EventClass, int] = {}
    for labels, value in rows:
        event_class = EventClass(
            category=labels["category"],
            level=labels["level"],
            event_name=labels["event_name"],
            source=labels["source"],
        )
        counts[event_class] = int(value)
    return counts


def active_dismissals(conn: Any) -> list[Dismissal]:
    """Load class-wide active dismissals only.

    The v1 API rejects a non-NULL agent_id rather than subtracting it from a
    class-wide Loki aggregate incorrectly. A manually inserted future
    per-agent row therefore remains visible in history but has no arithmetic
    effect until the query grouping grows that dimension.
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, category, level, event_name, source, agent_id, dismissed_by, note
            FROM event_dismissals
            WHERE status = 'dismissed' AND agent_id IS NULL
            """
        )
        rows = cur.fetchall()
    return [
        Dismissal(
            id=row["id"],
            event_class=EventClass(
                category=row["category"],
                level=row["level"],
                event_name=row["event_name"],
                source=row["source"],
            ),
            dismissed_by=row["dismissed_by"],
            note=row["note"],
        )
        for row in rows
    ]


def _reopen_for_burst(conn: Any, dismissal: Dismissal, count: int) -> bool:
    """Atomically flip one active dismissal; False means another actor won."""

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_dismissals
            SET status = 'reopened', reopened_at = now(), burst_count = %s, updated_at = now()
            WHERE id = %s AND status = 'dismissed'
            RETURNING id
            """,
            (count, dismissal.id),
        )
        return cur.fetchone() is not None


def _insert_auto_dismissal(conn: Any, event_class: EventClass, days: int) -> bool:
    """Insert one system dismissal if no concurrent resolution already did."""

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_dismissals
                (category, level, event_name, source, agent_id, dismissed_by, note)
            VALUES (%s, %s, %s, %s, NULL, -1, %s)
            ON CONFLICT (category, level, event_name, source, agent_id)
                WHERE status = 'dismissed' DO NOTHING
            RETURNING id
            """,
            (
                event_class.category,
                event_class.level,
                event_class.event_name,
                event_class.source,
                f"auto:stable-{days}-days",
            ),
        )
        return cur.fetchone() is not None


def _stable_auto_classes(now: datetime, current: dict[EventClass, int]) -> set[EventClass]:
    """Classes non-empty in every six-hour slice of the configured history.

    This is deliberately a small daily scan and default-off. It does not add a
    second persistence table: the recent Loki window is the observed history,
    and the partial unique index makes a restart's same-day repeat harmless.
    """

    if not settings.daemon.events_auto_dismiss_enabled:
        return set()
    if _last_auto_dismiss_day[0] == now.date():
        return set()

    slots = settings.daemon.events_auto_dismiss_days * 4
    stable = {event_class for event_class, count in current.items() if count > 0}
    for slot in range(1, slots):
        observed = _query_class_counts(_UNRESOLVED_WINDOW, now - slot * _AUTO_SLICE)
        stable &= {event_class for event_class, count in observed.items() if count > 0}
        if not stable:
            break
    _last_auto_dismiss_day[0] = now.date()
    return stable


def _resolution_attributes(
    event_class: EventClass, *, dismissed_by: int, note: str
) -> dict[str, object]:
    return {
        "category": event_class.category,
        "level": event_class.level,
        "event_name": event_class.event_name,
        "source": event_class.source,
        "agent_id": event_class.agent_id,
        "dismissed_by": dismissed_by,
        "note": note,
    }


def _marker_name(event_class: EventClass, action: str) -> str:
    prefix = "warning" if event_class.level == "warning" else "error"
    return f"{prefix}_{action}"


def _emit_reopened(dismissal: Dismissal, count: int) -> None:
    attributes = _resolution_attributes(
        dismissal.event_class, dismissed_by=dismissal.dismissed_by, note="auto:burst"
    )
    attributes.update({"reopened_by": "system:burst", "triggered_by_count": count})
    telemetry.emit(
        "telemetry",
        _marker_name(dismissal.event_class, "reopened"),
        source="system",
        attributes=attributes,
    )


def _emit_auto_resolved(event_class: EventClass, days: int) -> None:
    telemetry.emit(
        "telemetry",
        _marker_name(event_class, "resolved"),
        source="system",
        attributes=_resolution_attributes(
            event_class, dismissed_by=-1, note=f"auto:stable-{days}-days"
        ),
    )


def level_splits(counts: dict[EventClass, int], active: set[EventClass]) -> dict[str, LevelSplit]:
    """The window-agnostic class arithmetic: per-level total / dismissed / net.

    Every class in ``counts`` contributes its count to its level's ``total``;
    a class with an active dismissal moves the count from ``net`` to
    ``dismissed`` instead. Levels are ``"warning"`` and ``"error"`` —
    ``critical`` classes fold into ``error`` exactly as the Loki query's
    level domain (``warning|error|critical``) and the operator gauges do, so
    the three-way split always sums to the raw level counts.

    This is the single arithmetic both the daemon's fixed-window gauges and
    the dashboard's user-selected window use (task #1935).
    """

    splits: dict[str, LevelSplit] = {}
    for event_class, count in counts.items():
        level = "warning" if event_class.level == "warning" else "error"
        split = splits.get(level, LevelSplit(0, 0, 0))
        dismissed = count if event_class in active else 0
        splits[level] = LevelSplit(
            total=split.total + count,
            dismissed=split.dismissed + dismissed,
            net=split.net + count - dismissed,
        )
    return splits


def run_resolution_slice(
    pool: ConnectionPool, *, now: datetime | None = None
) -> ResolutionResult | None:
    """Run one fixed-window resolution pass, or return None when Loki is unsafe.

    An empty six-hour result is deliberately not emitted as two zero gauges:
    it is indistinguishable from a broken/empty query in this context, and a
    stale last-good Prometheus value is more honest than a fabricated recovery.
    """

    at = now or datetime.now(UTC)
    try:
        unresolved_counts = _query_class_counts(_UNRESOLVED_WINDOW, at)
        if not unresolved_counts:
            _log.warning("resolution query returned no six-hour classes; gauge not emitted")
            return None
        burst_counts = _query_class_counts(_BURST_WINDOW, at)
        auto_classes = _stable_auto_classes(at, unresolved_counts)
    except Exception:
        _log.warning("resolution Loki query failed; gauge not emitted", exc_info=True)
        return None

    reopened: list[tuple[Dismissal, int]] = []
    auto_dismissed: list[EventClass] = []
    with pool.connection() as conn:
        active = active_dismissals(conn)
        active_classes = {dismissal.event_class for dismissal in active}
        for dismissal in active:
            burst_count = burst_counts.get(dismissal.event_class, 0)
            if (
                burst_count > settings.daemon.events_resolution_burst_threshold
                and _reopen_for_burst(conn, dismissal, burst_count)
            ):
                reopened.append((dismissal, burst_count))
                active_classes.discard(dismissal.event_class)
        for event_class in auto_classes - active_classes:
            if _insert_auto_dismissal(conn, event_class, settings.daemon.events_auto_dismiss_days):
                auto_dismissed.append(event_class)
                active_classes.add(event_class)
        conn.commit()

    for dismissal, burst_count in reopened:
        _emit_reopened(dismissal, burst_count)
    for event_class in auto_dismissed:
        _emit_auto_resolved(event_class, settings.daemon.events_auto_dismiss_days)

    splits = level_splits(unresolved_counts, active_classes)
    warning = splits.get("warning", LevelSplit(0, 0, 0))
    error = splits.get("error", LevelSplit(0, 0, 0))
    telemetry.emit(
        "telemetry",
        "resolution_status",
        source="events-maintenance",
        attributes={
            "unresolved_warnings": warning.net,
            "unresolved_errors": error.net,
            "dismissed_warnings": warning.dismissed,
            "dismissed_errors": error.dismissed,
            "window": _UNRESOLVED_WINDOW,
        },
    )
    return ResolutionResult(
        unresolved_warnings=warning.net,
        unresolved_errors=error.net,
        reopened=len(reopened),
        auto_dismissed=len(auto_dismissed),
    )
