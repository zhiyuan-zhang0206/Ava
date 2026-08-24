"""Event-stream index-label rollout boundary and LogQL selector construction.

``INDEX_LABEL_CUTOVER_AT`` must be set to the UTC whole-hour boundary of the
collector rollout. Ship code with a future value first so every read remains on
the legacy selector; after the collector promotes the two resource attributes,
set this one constant to the rollout instant. The short legacy grace is the
only retention arithmetic in the codebase: once it expires, no normal event
stream row can lack the indexed labels.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import cast

EVENT_STREAM_SERVICE_NAME = "unknown_service"

# Operator-set deployment boundary. Keep this value in the future until the
# collector transform and Loki mapping are live cluster-wide.
INDEX_LABEL_CUTOVER_AT = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
EVENT_STREAM_RETENTION = timedelta(hours=168)
# Must match deployed Loki `querier.max_concurrent`; render validation catches
# drift before it ships (the 2026-08-18 incident).
LOKI_QUERY_CONCURRENCY = 4
LEGACY_READ_MARGIN = timedelta(minutes=10)
LEGACY_READ_EXPIRES_AT = INDEX_LABEL_CUTOVER_AT + EVENT_STREAM_RETENTION + LEGACY_READ_MARGIN
_LOGQL_REGEX_META = frozenset(".\\*+?()|[]{}^$")


class LokiReadEra(StrEnum):
    """Whether a query slice reads records before or after label promotion."""

    LEGACY = "legacy"
    INDEXED = "indexed"


@dataclass(frozen=True)
class LokiReadSlice:
    """One non-overlapping event-time interval and its available labels."""

    era: LokiReadEra
    start: datetime
    end: datetime


@dataclass(frozen=True)
class LedgerGapPlan:
    """The ledger/live split around the newest retained day (P1-③)."""

    gap_live: bool
    day_lt: date | None
    tail_from: datetime


def retention_floor(now: datetime | None = None) -> datetime:
    """Lower bound for Loki lookups: now - EVENT_STREAM_RETENTION."""

    return (now if now is not None else datetime.now(UTC)) - EVENT_STREAM_RETENTION


def ledger_gap_plan(newest_day: date | None, floor: datetime) -> LedgerGapPlan:
    """Plan one ledger/live split for every ledger-plus-tail reader.

    The newest rolled day remains live-re-readable while its midnight is in
    Loki retention, so its stale ledger row is excluded and the tail starts at
    that midnight. Older ledger rows are final; their tail starts after the
    newest day, clamped to the retained floor.
    """

    if newest_day is None:
        return LedgerGapPlan(gap_live=False, day_lt=None, tail_from=floor)
    newest_midnight = datetime.combine(newest_day, time.min, tzinfo=UTC)
    if newest_midnight >= floor:
        return LedgerGapPlan(gap_live=True, day_lt=newest_day, tail_from=newest_midnight)
    return LedgerGapPlan(
        gap_live=False,
        day_lt=None,
        tail_from=max(
            datetime.combine(newest_day + timedelta(days=1), time.min, tzinfo=UTC), floor
        ),
    )


def _retention_period_str() -> str:
    """Format the retention constant in Loki's whole-hour YAML syntax."""

    return f"{int(EVENT_STREAM_RETENTION.total_seconds() // 3600)}h"


def validate_loki_deploy_config(config: Mapping[str, object]) -> None:
    """Reject rendered Loki retention or query-capacity drift."""

    raw_limits_config = config["limits_config"]
    raw_querier = config["querier"]
    if not isinstance(raw_limits_config, Mapping) or not isinstance(raw_querier, Mapping):
        raise TypeError("Loki deploy config must contain limits_config and querier mappings")
    limits_config = cast(Mapping[str, object], raw_limits_config)
    querier = cast(Mapping[str, object], raw_querier)
    retention_period = limits_config["retention_period"]
    if retention_period != _retention_period_str():
        raise ValueError(
            f"Loki retention_period must be {_retention_period_str()!r}, got {retention_period!r}"
        )
    max_concurrent = querier["max_concurrent"]
    if max_concurrent != LOKI_QUERY_CONCURRENCY:
        raise ValueError(
            f"Loki querier.max_concurrent must be {LOKI_QUERY_CONCURRENCY}, got {max_concurrent!r}"
        )


def escape_logql_label(value: object) -> str:
    """Escape a value interpolated into a quoted LogQL matcher or filter."""

    return (
        str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    )


def split_index_label_window(
    start: datetime, end: datetime, *, now: datetime | None = None
) -> tuple[LokiReadSlice, ...]:
    """Partition ``(start, end]`` between legacy and indexed event streams.

    An exact boundary event belongs to the legacy slice. Once the fixed
    retention period plus grace has elapsed, legacy code is unreachable even
    when callers retain an older lower bound.
    """

    if start >= end:
        return ()
    current = now if now is not None else datetime.now(UTC)
    if current >= LEGACY_READ_EXPIRES_AT:
        return (LokiReadSlice(LokiReadEra.INDEXED, start, end),)
    if end <= INDEX_LABEL_CUTOVER_AT:
        return (LokiReadSlice(LokiReadEra.LEGACY, start, end),)
    if start >= INDEX_LABEL_CUTOVER_AT:
        return (LokiReadSlice(LokiReadEra.INDEXED, start, end),)
    return (
        LokiReadSlice(LokiReadEra.LEGACY, start, INDEX_LABEL_CUTOVER_AT),
        LokiReadSlice(LokiReadEra.INDEXED, INDEX_LABEL_CUTOVER_AT, end),
    )


def event_stream_selector(
    *,
    era: LokiReadEra,
    agent_id: int | None,
    event_names: Sequence[str] | None,
    legacy_unlabeled: bool = False,
    indexed_labeled: bool = False,
) -> str:
    """Build the one event-stream selector shared by every supported reader.

    The archive stream is a separate 365d retention bucket and is deliberately
    never read by this shared event-stream selector. Parity/import tooling that
    needs it builds its own selector. When both label flags are true, legacy
    and indexed eras are disjoint by label presence. Time-partitioned readers
    leave legacy unrestricted and use the indexed flag only.
    """

    matchers = [f'service_name="{EVENT_STREAM_SERVICE_NAME}"', 'stream!="archive"']
    if era is LokiReadEra.LEGACY:
        if legacy_unlabeled:
            matchers.append('event_name=""')
    else:
        if indexed_labeled:
            matchers.append('event_name!=""')
        if agent_id is not None:
            matchers.append(f'agent_id="{agent_id}"')
        if event_names:
            if len(event_names) == 1 and not _has_logql_regex(event_names[0]):
                matchers.append(f'event_name="{escape_logql_label(event_names[0])}"')
            else:
                joined = "|".join(escape_logql_label(name) for name in event_names)
                matchers.append(f'event_name=~"{joined}"')
    return "{" + ", ".join(matchers) + "}"


def _has_logql_regex(value: str) -> bool:
    """Whether an event-name filter needs regex rather than equality syntax."""

    return any(char in _LOGQL_REGEX_META for char in value)
