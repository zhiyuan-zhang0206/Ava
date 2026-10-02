"""Immutable event-class resolution — event-class counts -> dismissal state -> gauges.

The event record is append-only, so no resolution path may write a
``resolved_by`` attribute back onto historical events. Active rows in
``event_dismissals`` instead remove one exact (category, level, event_name,
source, process) class from the count — a row whose ``process`` is empty is a
wildcard that cancels every process of its base, the scope every pre-dimension
row keeps (task #4329 B5). The companion ten-minute query is the safety
valve: a renewed burst reopens the class before it can hide a new incident.

The class arithmetic is window-agnostic: :func:`level_splits` turns any
window's per-class counts plus the active dismissals into per-level
total / dismissed / net triples. The daemon's :func:`run_resolution_slice`
applies it to its fixed six-hour window and publishes the unresolved and
dismissed gauges; the gateway stats dashboard (``gateway/cluster/status.py``)
applies the same arithmetic to the frontend-selected window, so the two
surfaces agree class for class.

The counts are read from `telemetry_events` by :func:`class_counts`. The public seams are
:func:`run_resolution_slice`, :func:`level_splits`, :func:`class_counts` and
:func:`active_dismissals`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base import telemetry
from services.events_maintenance.config import EventsMaintenanceConfig

_log = logging.getLogger("services.events_maintenance.resolution")

_UNRESOLVED_WINDOW = timedelta(hours=6)
_BURST_WINDOW = timedelta(minutes=10)
_AUTO_SLICE = timedelta(hours=6)
# The newest recorded event may lag now by this much before the counts are not trusted: a stalled
# write path reads as an empty window, which must not be published as a recovery.
_MAX_RECORD_LAG = timedelta(minutes=15)


@dataclass(frozen=True)
class EventClass:
    """The immutable-event resolution identity (per-agent scope is reserved).

    ``process`` is the emitting-process dimension (task #4329 B5): ``""``
    reads as "every process" on a dismissal row and as "the row's body had no
    process" on the count side — the mixed-version read both maps to the
    wildcard.
    """

    category: str
    level: str
    event_name: str
    source: str
    process: str = ""
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


def class_counts(
    conn: Any, *, start: datetime, end: datetime, cluster: str | None = None
) -> dict[EventClass, int]:
    """Counts of the warning, error and critical event classes over `(start, end]`.

    Read from `telemetry_events`; `category` is always telemetry or log there. ``cluster``
    keeps the rows of that cluster and the rows with no label (the same acceptance as the
    other dashboard reads). The level predicate is a literal so the partial index on those
    levels applies.
    """
    query = """
        SELECT category, level, event_name, source, process, count(*)
        FROM telemetry_events
        WHERE level IN ('warning', 'error', 'critical')
          AND ts > %s AND ts <= %s
    """
    params: list[Any] = [start, end]
    if cluster is not None:
        query += " AND (cluster = %s OR cluster = '')"
        params.append(cluster)
    query += " GROUP BY category, level, event_name, source, process"
    return {
        EventClass(
            category=row[0], level=row[1], event_name=row[2], source=row[3], process=row[4]
        ): int(row[5])
        for row in conn.execute(query, params).fetchall()
    }


def active_dismissals(conn: Any) -> list[Dismissal]:
    """Load active class dismissals only (agent-scoped rows excluded).

    An empty ``process`` is a wildcard pattern; a concrete one targets a
    single emitting process (task #4329 B5). The v1 API rejects a non-NULL
    agent_id rather than subtracting it from a class-wide Loki aggregate
    incorrectly; a manually inserted future per-agent row therefore remains
    visible in history but has no arithmetic effect until the query grouping
    grows that dimension.
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, category, level, event_name, source, process, agent_id,
                   dismissed_by, note
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
                process=row["process"],
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
                (category, level, event_name, source, process, agent_id, dismissed_by, note)
            VALUES (%s, %s, %s, %s, %s, NULL, -1, %s)
            ON CONFLICT (category, level, event_name, source, process, agent_id)
                WHERE status = 'dismissed' DO NOTHING
            RETURNING id
            """,
            (
                event_class.category,
                event_class.level,
                event_class.event_name,
                event_class.source,
                event_class.process,
                f"auto:stable-{days}-days",
            ),
        )
        return cur.fetchone() is not None


def _stable_auto_classes(
    now: datetime, conn: Any, current: dict[EventClass, int], config: EventsMaintenanceConfig
) -> set[EventClass]:
    """Classes non-empty in every six-hour slice of the configured history.

    This is deliberately a small daily scan and default-off. It does not add a
    second persistence table: the recorded history is the observation, and the
    partial unique index makes a restart's same-day repeat harmless.
    """

    if not config.events_auto_dismiss_enabled:
        return set()
    if _last_auto_dismiss_day[0] == now.date():
        return set()

    slots = config.events_auto_dismiss_days * 4
    rows = conn.execute(
        """
        SELECT category, level, event_name, source, process
        FROM telemetry_events
        WHERE level IN ('warning', 'error', 'critical') AND ts > %s AND ts <= %s
        GROUP BY category, level, event_name, source, process
        HAVING count(DISTINCT floor(extract(epoch FROM (%s::timestamptz - ts)) / %s)) = %s
        """,
        (now - slots * _AUTO_SLICE, now, now, _AUTO_SLICE.total_seconds(), slots),
    ).fetchall()
    stable = {
        EventClass(category=r[0], level=r[1], event_name=r[2], source=r[3], process=r[4])
        for r in rows
    }
    _last_auto_dismiss_day[0] = now.date()
    return stable & {event_class for event_class, count in current.items() if count > 0}


def _resolution_attributes(
    event_class: EventClass, *, dismissed_by: int, note: str
) -> dict[str, object]:
    return {
        "category": event_class.category,
        "level": event_class.level,
        "event_name": event_class.event_name,
        "source": event_class.source,
        "process": event_class.process,
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


def _same_base(left: EventClass, right: EventClass) -> bool:
    """Whether two classes share the four base fields (process aside)."""

    return (left.category, left.level, left.event_name, left.source) == (
        right.category,
        right.level,
        right.event_name,
        right.source,
    )


def _is_dismissed(event_class: EventClass, active: set[EventClass]) -> bool:
    """Whether any active row cancels this counted class.

    An exact row (same process) or a wildcard row (``process=""`` — the scope
    every pre-dimension dismissal keeps) matches. A counted class whose
    ``process`` is empty (mixed-version read) matches only the wildcard: no
    live process can be attributed to it.
    """

    return event_class in active or replace(event_class, process="") in active


def _burst_count_for(event_class: EventClass, burst_counts: dict[EventClass, int]) -> int:
    """The ten-minute count one dismissal watches.

    An exact dismissal watches its own class; a wildcard dismissal watches
    the sum over every process of its base, so any process's burst still
    trips the safety valve.
    """

    if event_class.process:
        return burst_counts.get(event_class, 0)
    return sum(count for other, count in burst_counts.items() if _same_base(other, event_class))


def level_splits(counts: dict[EventClass, int], active: set[EventClass]) -> dict[str, LevelSplit]:
    """The window-agnostic class arithmetic: per-level total / dismissed / net.

    Every class in ``counts`` contributes its count to its level's ``total``;
    a class with an active dismissal moves the count from ``net`` to
    ``dismissed`` instead — an exact (process-scoped) row or a wildcard
    (``process=""``) row, see :func:`_is_dismissed`. Levels are ``"warning"``
    and ``"error"`` — ``critical`` classes fold into ``error`` exactly as the
    Loki query's level domain (``warning|error|critical``) and the operator
    gauges do, so the three-way split always sums to the raw level counts.

    This is the single arithmetic both the daemon's fixed-window gauges and
    the dashboard's user-selected window use (task #1935).
    """

    splits: dict[str, LevelSplit] = {}
    for event_class, count in counts.items():
        level = "warning" if event_class.level == "warning" else "error"
        split = splits.get(level, LevelSplit(0, 0, 0))
        dismissed = count if _is_dismissed(event_class, active) else 0
        splits[level] = LevelSplit(
            total=split.total + count,
            dismissed=split.dismissed + dismissed,
            net=split.net + count - dismissed,
        )
    return splits


def _read_window_counts(
    pool: ConnectionPool, config: EventsMaintenanceConfig, at: datetime
) -> tuple[dict[EventClass, int], dict[EventClass, int], set[EventClass]] | None:
    """The six-hour and ten-minute class counts and the stable auto-dismiss classes at `at`,
    or None when the newest recorded event is too old to trust an empty window."""
    with pool.connection() as conn:
        newest = conn.execute("SELECT max(ts) FROM telemetry_events").fetchone()
        if newest is None or newest[0] is None or at - newest[0] > _MAX_RECORD_LAG:
            _log.warning("telemetry_events is not current; resolution gauge not emitted")
            return None
        unresolved_counts = class_counts(conn, start=at - _UNRESOLVED_WINDOW, end=at)
        burst_counts = class_counts(conn, start=at - _BURST_WINDOW, end=at)
        return (
            unresolved_counts,
            burst_counts,
            _stable_auto_classes(at, conn, unresolved_counts, config),
        )


def run_resolution_slice(
    pool: ConnectionPool, config: EventsMaintenanceConfig, *, now: datetime | None = None
) -> ResolutionResult | None:
    """Run one fixed-window resolution pass, or return None when the record is stale.

    An empty six-hour result is a legitimate "no warnings or errors" when the record is
    current, so the gauges are emitted; when the newest recorded event is older than
    `_MAX_RECORD_LAG` the write path may be down and the window reads empty for that
    reason, so no gauge is emitted (a stale last-good Prometheus value is more honest than
    a fabricated recovery).
    """

    at = now or datetime.now(UTC)
    try:
        counts = _read_window_counts(pool, config, at)
    except Exception:
        _log.warning("resolution query failed; gauge not emitted", exc_info=True)
        return None
    if counts is None:
        return None
    unresolved_counts, burst_counts, auto_classes = counts

    reopened: list[tuple[Dismissal, int]] = []
    auto_dismissed: list[EventClass] = []
    with pool.connection() as conn:
        active = active_dismissals(conn)
        active_classes = {dismissal.event_class for dismissal in active}
        for dismissal in active:
            burst_count = _burst_count_for(dismissal.event_class, burst_counts)
            if burst_count > config.events_resolution_burst_threshold and _reopen_for_burst(
                conn, dismissal, burst_count
            ):
                reopened.append((dismissal, burst_count))
                active_classes.discard(dismissal.event_class)
        for event_class in auto_classes:
            if _is_dismissed(event_class, active_classes):
                continue
            if _insert_auto_dismissal(conn, event_class, config.events_auto_dismiss_days):
                auto_dismissed.append(event_class)
                active_classes.add(event_class)
        conn.commit()

    for dismissal, burst_count in reopened:
        _emit_reopened(dismissal, burst_count)
    for event_class in auto_dismissed:
        _emit_auto_resolved(event_class, config.events_auto_dismiss_days)

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
            "window": "6h",
        },
    )
    return ResolutionResult(
        unresolved_warnings=warning.net,
        unresolved_errors=error.net,
        reopened=len(reopened),
        auto_dismissed=len(auto_dismissed),
    )
