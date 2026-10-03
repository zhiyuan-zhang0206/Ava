"""LogQL builders and live-event read-window planning.

All query families use these helpers so filtering, live selector choice, and
quoted label escaping stay identical at every Loki read boundary.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from base.events.contract import TIER_BY_EVENT, EventTier
from base.telemetry.loki_index_labels import (
    LokiReadEra,
    archive_stream_selector,
    escape_logql_label,
    event_stream_selector,
)

_LEVELS = ("debug", "info", "warning", "error", "critical")
_ANOMALY_LEVELS = ("warning", "error", "critical")


def _escape_label(value: Any) -> str:
    """Escape a value interpolated into a LogQL label filter.

    Backslashes and double quotes are escaped; literal newlines (which
    cannot appear inside a LogQL quoted string) collapse to a space.
    """
    return escape_logql_label(value)


def _tier_event_names(tier: EventTier) -> tuple[str, ...]:
    """Declared names for one default tier, in stable LogQL-regex order."""
    return tuple(sorted(name for name, declared in TIER_BY_EVENT.items() if declared == tier))


def _event_name_regex(event_names: tuple[str, ...]) -> str:
    """Escaped alternation matching exactly the supplied event names."""
    return "|".join(_escape_label(re.escape(name)) for name in event_names)


def _tier_predicate(tiers: list[EventTier]) -> str:
    """One LogQL label-filter expression for a union of event tiers.

    Tier is derived from row fields rather than stored in Loki. The predicate
    mirrors ``base.events.contract.tier_for`` exactly, including severity
    and audit precedence, so filtering happens before pagination and count
    aggregation rather than after a page has been fetched.
    """
    anomaly_levels = "|".join(_ANOMALY_LEVELS)
    non_anomaly = f'level!~"{anomaly_levels}"'
    anomaly_names = _event_name_regex(_tier_event_names("anomaly"))
    non_observation_names = _event_name_regex(
        tuple(sorted(name for name, tier in TIER_BY_EVENT.items() if tier != "observation"))
    )
    clauses: list[str] = []
    for tier in tiers:
        if tier == "business":
            clauses.append(f'({non_anomaly} and category="audit")')
        elif tier == "anomaly":
            clauses.append(
                f'(level=~"{anomaly_levels}" or '
                f'({non_anomaly} and category!="audit" and event_name=~"{anomaly_names}"))'
            )
        elif tier == "noise":
            noise_names = _event_name_regex(_tier_event_names("noise"))
            clauses.append(f'({non_anomaly} and category!="audit" and event_name=~"{noise_names}")')
        else:
            clauses.append(
                f'({non_anomaly} and category!="audit" and event_name!~"{non_observation_names}")'
            )
    return "(" + " or ".join(clauses) + ")"


def _build_logql(
    *,
    era: LokiReadEra = LokiReadEra.LEGACY,
    archive: bool = False,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    drop_json_errors: bool = False,
) -> str:
    """LogQL for the event-history slice. Line filters (`|=`) come before
    the `| json` stage; json-parsed label filters after it.

    A cluster filter accepts both the requested cluster and a missing cluster
    field. Unlabeled rows predate cluster labeling and therefore belong to the
    single-cluster deployment's local history; labeled rows remain isolated.

    ``attribute_filters`` matches on **nested payload keys** (the event
    `attributes` object), e.g. `{"ok": "true"}`. Each key needs its own
    `| json k="attributes.k"` stage (multiple extractions in one stage are a
    LogQL parse error), followed by the comparison stage; a value prefixed
    `!=` becomes a negated match (`{"node": "!=claim"}` → `| node!="claim"`).

    ``exclude_agent_ids`` drops a closed set of agents
    (`| agent_id_extracted!~"a|b"`) while keeping service rows — the
    since_compact rest-partition filter (metrics aggregate path). It is
    mutually exclusive with ``agent_id``.

    With ``drop_json_errors`` the `| json` stage is followed by
    `| __error__=""`, dropping lines that failed to parse — the exact-total
    count path needs it so `count_over_time` and the row-parse path agree
    (both exclude unparseable lines).

    With ``archive=True`` the query targets the task #1281 archive stream
    (all pre-cutover events) instead of the live event stream: the archive
    has no event_name/agent_id index labels, so those filters match the
    plain json-extracted fields; ``era`` is ignored.
    """
    if archive:
        # The archive stream carries no event_name/agent_id index labels, so
        # `| json` extracts those fields plain (no `_extracted` suffix — the
        # live stream's labels collide and suffix them). Everything else in
        # the pipeline (category/level/cluster/attribute filters) is identical
        # between the two streams.
        selector = archive_stream_selector()
        event_name_field = "event_name"
        agent_id_field = "agent_id"
    else:
        selector = event_stream_selector(
            era=era,
            agent_id=agent_id,
            event_names=event_names,
        )
        event_name_field = "event_name_extracted"
        agent_id_field = "agent_id_extracted"
    parts = [selector]
    if grep:
        parts.append(f'|= "{_escape_label(grep)}"')
    parts.append("| json")
    if cluster is not None:
        parts.append(f'| cluster="{_escape_label(cluster)}" or cluster=""')
    if drop_json_errors:
        parts.append('| __error__=""')
    if attribute_filters:
        for key, value in attribute_filters.items():
            parts.append(f'| json {_escape_label(key)}="attributes.{_escape_label(key)}"')
            if value.startswith("!="):
                parts.append(f'| {_escape_label(key)}!="{_escape_label(value[2:])}"')
            else:
                parts.append(f'| {_escape_label(key)}="{_escape_label(value)}"')
    if agent_id is not None:
        parts.append(f'| {agent_id_field}="{agent_id}"')
    elif exclude_agent_ids:
        # since_compact partitioning: drop a closed set of agents, keep the
        # rest including service rows (no agent_id).
        joined = "|".join(str(a) for a in exclude_agent_ids)
        parts.append(f'| {agent_id_field}!~"{joined}"')
    elif service_only:
        # json turns a JSON null into an absent/empty field — empty matches.
        parts.append(f'| {agent_id_field}=""')
    if categories:
        joined = "|".join(_escape_label(x) for x in categories)
        parts.append(f'| category=~"{joined}"')
    if event_names:
        joined = "|".join(_escape_label(e) for e in event_names)
        parts.append(f'| {event_name_field}=~"{joined}"')
    if level_min is not None:
        idx = _LEVELS.index(level_min)
        joined = "|".join(_LEVELS[idx:])
        parts.append(f'| level=~"{joined}"')
    elif level is not None:
        parts.append(f'| level="{_escape_label(level)}"')
    if machine is not None:
        parts.append(f'| machine="{_escape_label(machine)}"')
    if trace_id is not None:
        parts.append(f'| trace_id="{_escape_label(trace_id.lower())}"')
    if tiers:
        parts.append(f"| {_tier_predicate(tiers)}")
    return " ".join(parts)


def _window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime] | None:
    """Resolve the effective window; None when it is empty (from >= to)."""
    now = datetime.now(UTC)
    start = from_ if from_ is not None else now - timedelta(hours=24)
    end = to if to is not None else now
    if start >= end:
        return None
    return start, end
