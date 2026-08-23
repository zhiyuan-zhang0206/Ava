"""Event-stream index-label rollout boundary and LogQL selector construction.

``INDEX_LABEL_CUTOVER_AT`` must be set to the UTC whole-hour boundary of the
collector rollout. Ship code with a future value first so every read remains on
the legacy selector; after the collector promotes the two resource attributes,
set this one constant to the rollout instant. The short legacy grace is the
only retention arithmetic in the codebase: once it expires, no normal event
stream row can lack the indexed labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

EVENT_STREAM_SERVICE_NAME = "unknown_service"

# Operator-set deployment boundary. Keep this value in the future until the
# collector transform and Loki mapping are live cluster-wide.
INDEX_LABEL_CUTOVER_AT = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
EVENT_STREAM_RETENTION = timedelta(hours=168)
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
    legacy_streams_only: bool = False,
) -> str:
    """Build the one event-stream selector shared by every supported reader.

    ``legacy_streams_only`` is used only by range queries spanning the
    cutover. An empty equality matcher selects streams without the promoted
    resource label, preserving their original bucket grid while making the two
    era queries disjoint.
    """

    matchers = [f'service_name="{EVENT_STREAM_SERVICE_NAME}"']
    if era is LokiReadEra.LEGACY:
        if legacy_streams_only:
            matchers.append('event_name=""')
    else:
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
