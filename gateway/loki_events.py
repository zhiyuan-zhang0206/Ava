"""Loki-backed event-history queries — the LGTM read side of the unified emitter.

Task #1197: the PG `events` read path is replaced by Loki. The emitter
(`shared/telemetry_otlp._emit_log`) ships every event as an OTLP log whose
line body is the full event JSON and whose top-level fields (agent_id,
event_name, level, category, ...) ride as structured metadata. Querying is
therefore LogQL: stream selector + `| json` + label filters; each matching
line parses straight back to the EventRow shape.

Notes:
- Structured metadata is NOT index-label matched by `{...}` selectors — the
  `| json` stage is required before any event-field filter.
- Loki has no numeric row id and no offset paging: `query_events` pages in
  memory (`limit + offset + 1` rows fetched, offset slice taken) and derives
  a stable surrogate id per line (blake2b over ts+line). The JSONL mirror
  persists that same id alongside its row. `has_more` comes from the +1
  lookahead row.
- The `grep` filter maps to a line-content match (`|=`), which sees the full
  JSON body including the nested `attributes.msg` payload.
- `categories` is an OR regex (`category=~"telemetry|log"`); the events-table
  contract (telemetry/log only, audit excluded) is enforced by the callers.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, overload

import httpx

from gateway import loki_query_budget
from shared import telemetry
from shared.config import settings
from shared.events.contract import TIER_BY_EVENT, EventTier
from shared.log import logger
from shared.loki_index_labels import (
    LokiReadEra,
    LokiReadSlice,
    escape_logql_label,
    event_stream_selector,
    split_index_label_window,
)

# Minimum-level ordering (lowercase, matches the events-table convention).
_LEVELS = ("debug", "info", "warning", "error", "critical")
_ANOMALY_LEVELS = ("warning", "error", "critical")

# Loki's default per-query series cap; event queries over long windows can
# exceed it when the count path is used. The list path fetches raw streams
# so it is unaffected.
_HTTP_TIMEOUT_S = 60.0

# A query slower than this logs a structured "loki query slow" line — the
# threshold keeps the log volume near zero (fast queries are Loki's own
# metrics' business) while making the slow tail visible in the gateway log
# (which the LGTM sidecar ships into the event stream). Task #1289: the
# 2026-08-20 /api/events 500s were 60s Loki hangs this line would have caught.
_SLOW_QUERY_LOG_S = 5.0


def _log_loki_failure(
    *, endpoint: str, params: dict[str, Any], duration_s: float, error: str
) -> None:
    """Emit a structured `loki_query_failed` event for a transport failure.

    The router maps the re-raised exception to a 503; this event is the
    durable record of *which* query shape stalled, for post-mortems when
    Loki's own logs have already rotated away (task #1289: the incident
    window's Loki logs were gone before anyone looked). Best-effort: an
    emit contract violation must never mask the original transport error.
    """
    window_from: str | None = None
    window_to: str | None = None
    start_ns = params.get("start")
    end_ns = params.get("end")
    if isinstance(start_ns, int):
        window_from = datetime.fromtimestamp(start_ns / 1e9, UTC).isoformat()
    if isinstance(end_ns, int):
        window_to = datetime.fromtimestamp(end_ns / 1e9, UTC).isoformat()
    attributes = {
        "endpoint": endpoint,
        "duration_s": round(duration_s, 3),
        "error": error,
        "window_from": window_from,
        "window_to": window_to,
        "query": str(params.get("query", ""))[:500],
    }
    try:
        telemetry.emit("log", "loki_query_failed", level="error", attributes=attributes)
    except Exception:
        logger.error("loki_query_failed emit failed", attributes=attributes)


def _get_json(
    url: str,
    params: dict[str, Any],
    *,
    endpoint: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """One Loki HTTP GET with per-call timing and failure reporting.

    Every Loki call in this module funnels through here. Transport failures
    (timeout / disconnect — httpx.TimeoutException / httpx.TransportError)
    and non-2xx responses (httpx.HTTPStatusError) emit a `loki_query_failed`
    event and re-raise untouched; the callers' routers map them to 503s.
    Slow-but-successful queries log one structured line (see
    `_SLOW_QUERY_LOG_S`).
    """
    started = time.perf_counter()
    try:
        with loki_query_budget.query_budget.slot():
            if timeout_s is None:
                resp = _client().get(url, params=params)
            else:
                resp = _client().get(url, params=params, timeout=timeout_s)
            resp.raise_for_status()
            payload = resp.json()
    except loki_query_budget.LokiQueryBudgetError:
        # A local admission refusal means the backend was never called. Its
        # own budget transition event/counters already identify queue_full vs
        # acquire_timeout; recording it as loki_query_failed would falsely
        # blame the transport and double-count one failure class.
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
        duration_s = time.perf_counter() - started
        _log_loki_failure(
            endpoint=endpoint,
            params=params,
            duration_s=duration_s,
            error=type(exc).__name__,
        )
        raise
    duration_s = time.perf_counter() - started
    if duration_s >= _SLOW_QUERY_LOG_S:
        logger.info(
            "loki query slow: {endpoint} {duration_s:.1f}s {query}",
            endpoint=endpoint,
            duration_s=round(duration_s, 1),
            query=str(params.get("query", ""))[:300],
        )
    return payload


# One long-lived HTTP client for every Loki query — connection reuse across
# the gateway's fan-out reads (an inspect poll or ops-monitor call issues
# dozens of queries; a per-call client opened a fresh TCP connection for each).
# The pool ceiling stays above the ops-series peak fan-out (~18 concurrent
# queries) so pool-acquire never times out under normal load.
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """The module's shared Loki client, created on first use (lazy so tests
    can swap this accessor before any real client exists)."""
    global _shared_client  # noqa: PLW0603 — process-level singleton
    if _shared_client is None:
        _shared_client = httpx.Client(
            timeout=_HTTP_TIMEOUT_S,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        )
    return _shared_client


def _escape_label(value: Any) -> str:
    """Escape a value interpolated into a LogQL label filter.

    Backslashes and double quotes are escaped; literal newlines (which
    cannot appear inside a LogQL quoted string) collapse to a space.
    """
    return escape_logql_label(value)


_event_id = telemetry.event_id


def _parse_line(line: str, ts_ns: int) -> dict[str, Any] | None:
    """Parse one Loki line back to the EventRow-shaped dict (id synthesized)."""
    try:
        obj: dict[str, Any] = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("attributes")
    ts_raw = obj.get("ts")
    try:
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.fromtimestamp(ts_ns / 1e9, UTC)
    except ValueError:
        ts = datetime.fromtimestamp(ts_ns / 1e9, UTC)
    return {
        "id": _event_id(line, ts_ns),
        "ts": ts,
        "trace_id": obj.get("trace_id"),
        "span_id": obj.get("span_id"),
        "agent_id": obj.get("agent_id"),
        "machine": obj.get("machine") or "",
        "process": obj.get("process") or "",
        "category": obj.get("category") or "",
        "event_name": obj.get("event_name") or "",
        "level": (obj.get("level") or "").lower(),
        "source": obj.get("source") or "",
        "target_agent_id": obj.get("target_agent_id"),
        "attributes": payload if isinstance(payload, dict) else {},
    }


def _tier_event_names(tier: EventTier) -> tuple[str, ...]:
    """Declared names for one default tier, in stable LogQL-regex order."""
    return tuple(sorted(name for name, declared in TIER_BY_EVENT.items() if declared == tier))


def _event_name_regex(event_names: tuple[str, ...]) -> str:
    """Escaped alternation matching exactly the supplied event names."""
    return "|".join(_escape_label(re.escape(name)) for name in event_names)


def _tier_predicate(tiers: list[EventTier]) -> str:
    """One LogQL label-filter expression for a union of event tiers.

    Tier is derived from row fields rather than stored in Loki. The predicate
    mirrors ``shared.events.contract.tier_for`` exactly, including severity
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
    legacy_streams_only: bool = False,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    drop_json_errors: bool = False,
) -> str:
    """LogQL for the event-history slice. Line filters (`|=`) come before
    the `| json` stage; json-parsed label filters after it.

    ``attribute_filters`` matches on **nested payload keys** (the event
    `attributes` object), e.g. `{"ok": "true"}`. Each key needs its own
    `| json k="attributes.k"` stage (multiple extractions in one stage are a
    LogQL parse error), followed by the comparison stage; a value prefixed
    `!=` becomes a negated match (`{"node": "!=claim"}` → `| node!="claim"`).

    ``exclude_agent_ids`` drops a closed set of agents (`| agent_id!~"a|b"`)
    while keeping service rows — the since_compact rest-partition filter
    (metrics aggregate path). It is mutually exclusive with ``agent_id``.

    With ``drop_json_errors`` the `| json` stage is followed by
    `| __error__=""`, dropping lines that failed to parse — the exact-total
    count path needs it so `count_over_time` and the row-parse path agree
    (both exclude unparseable lines).
    """
    parts = [
        event_stream_selector(
            era=era,
            agent_id=agent_id,
            event_names=event_names,
            legacy_streams_only=legacy_streams_only,
        )
    ]
    if grep:
        parts.append(f'|= "{_escape_label(grep)}"')
    parts.append("| json")
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
        parts.append(f'| agent_id="{agent_id}"')
    elif exclude_agent_ids:
        # since_compact partitioning: drop a closed set of agents, keep the
        # rest including service rows (no agent_id).
        joined = "|".join(str(a) for a in exclude_agent_ids)
        parts.append(f'| agent_id!~"{joined}"')
    elif service_only:
        # json turns a JSON null into an absent/empty field — empty matches.
        parts.append('| agent_id=""')
    if categories:
        joined = "|".join(_escape_label(x) for x in categories)
        parts.append(f'| category=~"{joined}"')
    if event_names:
        joined = "|".join(_escape_label(e) for e in event_names)
        parts.append(f'| event_name=~"{joined}"')
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


def query_events(
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
    timeout_s: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Slice of the event stream from Loki, newest-first by default.

    Returns (rows, has_more). ``offset`` pages in memory (Loki has no
    offset); the fetch is ``limit + offset + 1`` rows so ``has_more`` is
    exact. ``from_``/``to`` bound the window; ``from_`` defaults to
    now - 24h (same lower-bound contract as the old PG API). ``direction``
    is ``"backward"`` (newest first) or ``"forward"`` (oldest first — the
    aggregate path uses it for per-agent first-event timestamps). ``timeout_s``
    overrides the shared client's default for this request only.
    """
    window = _window(from_, to)
    if window is None:
        return [], False
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"

    slices = _read_slices(window)
    raw: list[tuple[int, str]] = []
    for slice_ in slices:
        logql = _build_logql(
            era=slice_.era,
            legacy_streams_only=len(slices) == 2 and slice_.era is LokiReadEra.LEGACY,
            agent_id=agent_id,
            exclude_agent_ids=exclude_agent_ids,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            tiers=tiers,
            machine=machine,
            trace_id=trace_id,
            attribute_filters=attribute_filters,
            drop_json_errors=tiers is not None,
        )
        params = {
            "query": logql,
            "limit": limit + offset + 1,  # +1 lookahead for has_more
            "direction": direction,
            "start": int(slice_.start.timestamp() * 1e9),
            "end": int(slice_.end.timestamp() * 1e9),
        }
        payload = _get_json(url, params, endpoint="query_range", timeout_s=timeout_s)
        for stream in payload.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                raw.append((int(ts_ns), line))
    # query_range groups by stream; each stream is already direction-sorted,
    # but cross-stream ordering needs one merge pass.
    raw.sort(key=lambda pair: pair[0], reverse=(direction == "backward"))

    seen: set[tuple[int, str]] = set()
    rows: list[dict[str, Any]] = []
    for ts_ns, line in raw:
        if (ts_ns, line) in seen:
            continue
        seen.add((ts_ns, line))
        parsed = _parse_line(line, ts_ns)
        if parsed is not None:
            rows.append(parsed)
            if len(rows) >= limit + offset + 1:
                break

    has_more = len(rows) > offset + limit
    return rows[offset : offset + limit], has_more


def _window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime] | None:
    """Resolve the effective window; None when it is empty (from >= to)."""
    now = datetime.now(UTC)
    start = from_ if from_ is not None else now - timedelta(hours=24)
    end = to if to is not None else now
    if start >= end:
        return None
    return start, end


def _read_slices(window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
    """The one event-time partition every Loki read uses during rollout."""

    return split_index_label_window(*window)


def _slice_duration_s(slice_: LokiReadSlice) -> int:
    """Range-vector duration for one half-open rollout slice."""

    return max(1, int((slice_.end - slice_.start).total_seconds()))


def count_events(
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> int:
    """Exact count of event lines matching the same filters as
    `query_events` — the opt-in (`with_total=1`) `meta.total` of `/api/events`.

    Loki has no cheap per-query line count that honors `| json`-parsed
    filters, so this runs `sum(count_over_time(...))` as an *instant* query
    at the window end: the range vector `[to - from]` evaluated at `to`
    counts exactly the lines in `[from, to]`, matching the list path's
    window. `| __error__=""` keeps count and row-parse semantics identical
    (unparseable lines are excluded from both). Verified against real Loki
    for every filter combination (task #1197 PR 2).
    """
    window = _window(from_, to)
    if window is None:
        return 0
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query"
    slices = _read_slices(window)
    total = 0
    for slice_ in slices:
        pipeline = _build_logql(
            era=slice_.era,
            legacy_streams_only=len(slices) == 2 and slice_.era is LokiReadEra.LEGACY,
            agent_id=agent_id,
            exclude_agent_ids=exclude_agent_ids,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            tiers=tiers,
            machine=machine,
            trace_id=trace_id,
            attribute_filters=attribute_filters,
            drop_json_errors=True,
        )
        logql = f"sum(count_over_time(({pipeline})[{_slice_duration_s(slice_)}s]))"
        payload = _get_json(url, {"query": logql, "time": slice_.end.timestamp()}, endpoint="query")
        result = payload.get("data", {}).get("result", [])
        if result:
            value = result[0].get("value") or result[0].get("values", [None])[-1]
            total += int(value[1]) if value else 0
    return total


def _agg_pipeline(
    *,
    era: LokiReadEra = LokiReadEra.LEGACY,
    legacy_streams_only: bool = False,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
) -> str:
    """Filter pipeline for the numeric-aggregation path — structured
    metadata filters directly (no `| json` — a plain json stage would
    flatten the nested `attributes` object into per-line labels and split
    the aggregation into per-line series), then one json stage per nested
    attribute filter."""
    parts = [
        event_stream_selector(
            era=era,
            agent_id=agent_id,
            event_names=event_names,
            legacy_streams_only=legacy_streams_only,
        )
    ]
    if grep:
        parts.append(f'|= "{_escape_label(grep)}"')
    if agent_id is not None:
        parts.append(f'| agent_id="{agent_id}"')
    elif exclude_agent_ids:
        joined = "|".join(str(a) for a in exclude_agent_ids)
        parts.append(f'| agent_id!~"{joined}"')
    elif service_only:
        parts.append('| agent_id=""')
    if categories:
        joined = "|".join(_escape_label(x) for x in categories)
        parts.append(f'| category=~"{joined}"')
    if event_names:
        joined = "|".join(_escape_label(e) for e in event_names)
        parts.append(f'| event_name=~"{joined}"')
    if level_min is not None:
        idx = _LEVELS.index(level_min)
        parts.append(f'| level=~"{"|".join(_LEVELS[idx:])}"')
    elif level is not None:
        parts.append(f'| level="{_escape_label(level)}"')
    if machine is not None:
        parts.append(f'| machine="{_escape_label(machine)}"')
    if trace_id is not None:
        parts.append(f'| trace_id="{_escape_label(trace_id.lower())}"')
    if attribute_filters:
        for key, value in attribute_filters.items():
            parts.append(f'| json {_escape_label(key)}="attributes.{_escape_label(key)}"')
            if value.startswith("!="):
                parts.append(f'| {_escape_label(key)}!="{_escape_label(value[2:])}"')
            else:
                parts.append(f'| {_escape_label(key)}="{_escape_label(value)}"')
    return " ".join(parts)


def _agg_pipelines(
    window: tuple[datetime, datetime],
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
) -> list[tuple[LokiReadSlice, str]]:
    """Build the same aggregation pipeline once for every rollout slice."""

    slices = _read_slices(window)
    return [
        (
            slice_,
            _agg_pipeline(
                era=slice_.era,
                legacy_streams_only=len(slices) == 2 and slice_.era is LokiReadEra.LEGACY,
                agent_id=agent_id,
                exclude_agent_ids=exclude_agent_ids,
                service_only=service_only,
                event_names=event_names,
                level_min=level_min,
                level=level,
                grep=grep,
                categories=categories,
                machine=machine,
                trace_id=trace_id,
                attribute_filters=attribute_filters,
            ),
        )
        for slice_ in slices
    ]


def _range_eras(window: tuple[datetime, datetime]) -> list[tuple[LokiReadEra, bool]]:
    """Read eras for a range query without shifting its caller-owned grid."""

    slices = _read_slices(window)
    return [
        (slice_.era, len(slices) == 2 and slice_.era is LokiReadEra.LEGACY) for slice_ in slices
    ]


def _quantile_aggregate(
    *,
    pipelines: list[tuple[LokiReadSlice, str]],
    field: str,
    quantile: float,
    group_by: str | None,
) -> float | list[tuple[str, float]]:
    """Quantile over a payload attribute. Loki's `quantile_over_time` is
    per-series and every event is its own series (`trace_id`/`span_id`), so
    the count-by-value distribution is fetched and the percentile is
    computed client-side (linear interpolation, SQL `percentile_cont`
    semantics).

    Loki caps a query's output series at `max_query_series` (default 500);
    a full-precision float field makes nearly every event a distinct value,
    so a whole-life window's exact distribution exceeds the cap and Loki
    rejects the query (inspector no-hours 500, 2026-08-13). On that specific
    rejection the distribution is re-fetched bucketed to integer seconds —
    cardinality bounded by the duration RANGE, not the event count — wrapped
    in `topk(500, ...)` as a hard cap; bucket midpoints stand in for the
    values."""
    group_stage = (
        f' | json {_escape_label(group_by)}="attributes.{_escape_label(group_by)}"'
        if group_by
        else ""
    )
    extract = f'{group_stage} | json {_escape_label(field)}="attributes.{_escape_label(field)}" | __error__=""'
    dist: list[dict[str, Any]] = []
    for slice_, base_pipeline in pipelines:
        pipeline = base_pipeline
        duration_s = _slice_duration_s(slice_)
        if group_by:
            logql = (
                f"sum by ({_escape_label(group_by)}, {_escape_label(field)}) ("
                f"count_over_time(({pipeline}{extract})[{duration_s}s]))"
            )
        else:
            logql = (
                f"sum by ({_escape_label(field)}) ("
                f"count_over_time(({pipeline}{extract})[{duration_s}s]))"
            )
        try:
            dist.extend(_query_instant(logql, slice_.end))
        except httpx.HTTPStatusError as exc:
            if not _is_series_limit(exc):
                raise
            bucketed = _query_instant(
                _bucketed_distribution_logql(
                    pipeline=pipeline,
                    extract=extract,
                    field=field,
                    group_by=group_by,
                    duration_s=duration_s,
                ),
                slice_.end,
            )
            _relabel_buckets(bucketed, field)
            dist.extend(bucketed)
    if group_by:
        groups: dict[str, list[tuple[float, int]]] = {}
        for series in dist:
            metric = series.get("metric", {})
            value = _result_value(series)
            if value is None:
                continue
            g = str(metric.get(group_by, ""))
            groups.setdefault(g, []).append((float(metric.get(field, 0)), int(value)))
        return [(g, _weighted_quantile(quantile, sorted(vals))) for g, vals in groups.items()]
    vals: list[tuple[float, int]] = []
    for series in dist:
        metric = series.get("metric", {})
        value = _result_value(series)
        if value is not None:
            vals.append((float(metric.get(field, 0)), int(value)))
    return _weighted_quantile(quantile, sorted(vals))


def _count_attribute_slices(
    *, pipelines: list[tuple[LokiReadSlice, str]], group_by: str | None
) -> float | list[tuple[str, float]]:
    """Count each cutover slice and add scalar or group totals."""

    group_stage = (
        f' | json {_escape_label(group_by)}="attributes.{_escape_label(group_by)}"'
        if group_by
        else ""
    )
    totals: dict[str, float] = {}
    scalar = 0.0
    for slice_, pipeline in pipelines:
        if group_by:
            logql = (
                f"sum by ({_escape_label(group_by)}) (count_over_time(({pipeline}{group_stage})"
                f"[{_slice_duration_s(slice_)}s]))"
            )
        else:
            logql = f"sum(count_over_time(({pipeline})[{_slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end):
            value = _result_value(series)
            if value is None:
                continue
            if group_by:
                key = str(series.get("metric", {}).get(group_by, ""))
                totals[key] = totals.get(key, 0.0) + value
            else:
                scalar += value
    return list(totals.items()) if group_by else scalar


def _numeric_attribute_slices(
    *,
    pipelines: list[tuple[LokiReadSlice, str]],
    field: str,
    agg: str,
    group_by: str | None,
    range_op: str,
    cross_op: str,
) -> float | list[tuple[str, float]]:
    """Merge sum/min/max payload aggregates across non-overlapping slices."""

    group_stage = (
        f' | json {_escape_label(group_by)}="attributes.{_escape_label(group_by)}"'
        if group_by
        else ""
    )
    totals: dict[str, float] = {}
    scalar: float | None = None
    for slice_, pipeline in pipelines:
        unwrap_pipe = (
            f'{pipeline}{group_stage} | json {_escape_label(field)}="attributes.{_escape_label(field)}" '
            f'| __error__="" | unwrap {_escape_label(field)}'
        )
        if group_by:
            logql = (
                f"{cross_op} by ({_escape_label(group_by)}) ("
                f"{range_op}(({unwrap_pipe})[{_slice_duration_s(slice_)}s]))"
            )
        else:
            logql = f"{cross_op}({range_op}(({unwrap_pipe})[{_slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end):
            value = _result_value(series)
            if value is None:
                continue
            if group_by:
                key = str(series.get("metric", {}).get(group_by, ""))
                prior = totals.get(key)
                if prior is None:
                    totals[key] = value
                elif agg == "sum":
                    totals[key] = prior + value
                elif agg == "min":
                    totals[key] = min(prior, value)
                else:
                    totals[key] = max(prior, value)
            elif scalar is None:
                scalar = value
            elif agg == "sum":
                scalar += value
            elif agg == "min":
                scalar = min(scalar, value)
            else:
                scalar = max(scalar, value)
    return list(totals.items()) if group_by else (scalar if scalar is not None else 0.0)


@overload
def attribute_aggregate(
    *,
    field: str,
    agg: str,
    quantile: float | None = None,
    group_by: str,
    agent_id: int | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> list[tuple[str, float]]: ...


@overload
def attribute_aggregate(
    *,
    field: str,
    agg: str,
    quantile: float | None = None,
    group_by: None = None,
    agent_id: int | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> float: ...


def attribute_aggregate(
    *,
    field: str,
    agg: str,
    quantile: float | None = None,
    group_by: str | None = None,
    agent_id: int | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> float | list[tuple[str, float]]:
    """Numeric aggregate over one **payload attribute** (nested in the event
    JSON's `attributes` object) — the Loki replacement for the SQL-side
    SUM/MIN/MAX/percentile aggregates of the old PG reads.

    `agg` is one of `sum` / `min` / `max` / `quantile` / `count`:
    - `sum`/`min`/`max` run `{sum,min,max}_over_time` over the unwrapped
      field as an instant query at the window end (like `count_events`),
      wrapped in the same-named cross-series aggregation;
    - `quantile` needs `quantile` (0..1): Loki's per-series
      `quantile_over_time` is meaningless here — the query is evaluated per
      series, and `trace_id`/`span_id` make every event its own series — so
      the count-by-value distribution is fetched and the percentile is
      computed client-side (linear interpolation, matching SQL
      `percentile_cont` semantics);
    - `count` counts matching LINES (no unwrap) — useful grouped per model.
    With `group_by` set the result is `[(group_value, value)]` — the payload
    attribute named by `group_by` is json-extracted as a label and the
    aggregation groups by it.

    Returns a float for scalar aggregates, `list[tuple[str, float]]` when
    `group_by` is set (empty list = no matching lines).

    Notes (verified against real Loki, task #1197):
    - the event filters (agent_id / event_name / level / ...) match Loki
      **structured metadata directly** — no `| json` stage is needed for
      them, and a plain `| json` must NOT be added here: it flattens the
      nested `attributes` object into per-line labels and would split the
      aggregation into per-line series;
    - the numeric field needs its own `| json f="attributes.f"` stage (one
      extraction per stage — multiple extractions are a parse error);
    - `unwrap` only parses inside range aggregations.
    """
    window = _window(from_, to)
    if window is None:
        return [] if group_by else 0.0
    pipelines = _agg_pipelines(
        window,
        agent_id=agent_id,
        service_only=service_only,
        event_names=event_names,
        level_min=level_min,
        level=level,
        grep=grep,
        categories=categories,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
    )

    if agg == "quantile":
        if quantile is None:
            raise ValueError("attribute_aggregate(agg='quantile') needs quantile")
        return _quantile_aggregate(
            pipelines=pipelines,
            field=field,
            quantile=quantile,
            group_by=group_by,
        )

    if agg == "count":
        return _count_attribute_slices(pipelines=pipelines, group_by=group_by)

    ops = {
        "sum": ("sum_over_time", "sum"),
        "min": ("min_over_time", "min"),
        "max": ("max_over_time", "max"),
    }
    if agg not in ops:
        raise ValueError(f"attribute_aggregate: unknown agg {agg!r}")
    range_op, cross_op = ops[agg]
    return _numeric_attribute_slices(
        pipelines=pipelines,
        field=field,
        agg=agg,
        group_by=group_by,
        range_op=range_op,
        cross_op=cross_op,
    )


def count_by_event_name(
    *,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime,
    to: datetime,
) -> dict[str, int]:
    """Count one agent's matching events, grouped by event name.

    This is intentionally narrower than :func:`count_events`: inspector reads
    use one grouped query for related counters, rather than a query per event
    shape. Callers split any span larger than three hours before using it.
    """
    window = _window(from_, to)
    if window is None:
        return {}
    pipelines = _agg_pipelines(
        window,
        agent_id=agent_id,
        event_names=event_names,
        categories=categories,
        attribute_filters=attribute_filters,
    )
    counts: dict[str, int] = {}
    for slice_, pipeline in pipelines:
        logql = f"sum by (event_name) (count_over_time(({pipeline})[{_slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end):
            value = _result_value(series)
            if value is not None:
                key = str(series.get("metric", {}).get("event_name", ""))
                counts[key] = counts.get(key, 0) + int(value)
    return counts


def attribute_distribution(
    *,
    field: str,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime,
    to: datetime,
) -> list[tuple[float, int]]:
    """Return one numeric payload-attribute distribution from a Loki span.

    The series-limit fallback is shared with ``attribute_aggregate`` so the
    inspector's merged percentiles retain its existing bounded-cardinality
    behavior. Callers merge the returned count buckets across small windows.
    """
    window = _window(from_, to)
    if window is None:
        return []
    pipelines = _agg_pipelines(
        window,
        agent_id=agent_id,
        event_names=event_names,
        categories=categories,
        attribute_filters=attribute_filters,
    )
    extract = f' | json {_escape_label(field)}="attributes.{_escape_label(field)}" | __error__=""'
    totals: dict[float, int] = {}
    for slice_, pipeline in pipelines:
        duration_s = _slice_duration_s(slice_)
        logql = f"sum by ({_escape_label(field)}) (count_over_time(({pipeline}{extract})[{duration_s}s]))"
        try:
            result = _query_instant(logql, slice_.end)
        except httpx.HTTPStatusError as exc:
            if not _is_series_limit(exc):
                raise
            result = _query_instant(
                _bucketed_distribution_logql(
                    pipeline=pipeline,
                    extract=extract,
                    field=field,
                    group_by=None,
                    duration_s=duration_s,
                ),
                slice_.end,
            )
            _relabel_buckets(result, field)
        for series in result:
            value = _result_value(series)
            if value is not None:
                key = float(series.get("metric", {}).get(field, 0))
                totals[key] = totals.get(key, 0) + int(value)
    return sorted(totals.items())


def _is_series_limit(exc: httpx.HTTPStatusError) -> bool:
    """Whether a Loki rejection is its `max_query_series` cap (the exact
    count-by-value distribution exceeded it) — the one error the bucketed
    fallback can answer."""
    return exc.response.status_code == 400 and "maximum number of series" in exc.response.text


def _bucketed_distribution_logql(
    *,
    pipeline: str,
    extract: str,
    field: str,
    group_by: str | None,
    duration_s: int,
) -> str:
    """The count-by-value distribution bucketed to integer seconds.

    `label_format` truncates the float at the decimal point (no arithmetic
    exists in LogQL templates); `topk(500, ...)` is a no-op while the bucket
    count stays under Loki's series cap and a graceful tail-drop beyond it
    (a turn lasting 500s+)."""
    bucket = f"{field}_bucket"
    bucket_stage = f'| label_format {bucket}="{{{{ regexReplaceAll \\"([0-9]+)[.][0-9]+\\" .{field} \\"$1\\" }}}}"'
    group = f"{_escape_label(group_by)}, " if group_by else ""
    return (
        f"topk(500, sum by ({group}{bucket}) ("
        f"count_over_time(({pipeline}{extract}{bucket_stage})[{duration_s}s])))"
    )


def _relabel_buckets(dist: list[dict[str, Any]], field: str) -> None:
    """Replace the integer-second bucket label with the field label carrying
    the bucket midpoint (the value representative for the client-side
    percentile); an empty bucket (line without the attribute) counts as 0,
    mirroring the exact path's missing-label grouping."""
    bucket = f"{field}_bucket"
    for series in dist:
        metric = series.get("metric", {})
        raw = metric.pop(bucket, None)
        if raw is None:
            continue
        metric[field] = str(float(raw) + 0.5) if raw != "" else "0"


def _query_instant(logql: str, at: datetime) -> list[dict[str, Any]]:
    """One Loki instant query; returns the result vectors (metric + value)."""
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query"
    params = {"query": logql, "time": at.timestamp()}
    payload = _get_json(url, params, endpoint="query")
    return payload.get("data", {}).get("result", [])


def metric_range(
    logql: str,
    *,
    from_: datetime,
    to: datetime,
    step_s: int,
) -> list[tuple[str, float]]:
    """Bucketed series from a rendered LogQL range query — the inspector's
    Loki execution path (task #1280).

    The query is a complete LogQL expression (Grafana macros already
    substituted) with a ``[$__interval]`` window; ``step_s`` must match that
    window so the buckets are non-overlapping. The FIRST result series folds
    to (bucket ISO ts, value) pairs — the inspector contract is one series
    per metric, and a multi-series template is a spec bug.

    Returns [] when Loki returns no series (no matching events in the
    window). Raises httpx.HTTPError / ValueError on transport, status, or
    payload problems — the caller reports them per-metric.
    """
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"
    params = {
        "query": logql,
        "start": int(from_.timestamp() * 1e9),
        "end": int(to.timestamp() * 1e9),
        "step": str(step_s),
        "limit": "2000",
    }
    payload = _get_json(url, params, endpoint="query_range")
    result = payload.get("data", {}).get("result", [])
    if not result:
        return []
    return [
        (datetime.fromtimestamp(int(ts_ns) / 1e9, UTC).isoformat(), float(v))
        for ts_ns, v in result[0].get("values", [])
    ]


def _result_value(series: dict[str, Any]) -> float | None:
    """The instant-query value of one result vector (None when absent)."""
    value = series.get("value") or series.get("values", [None])[-1]
    return float(value[1]) if value else None


def _weighted_quantile(q: float, vals: list[tuple[float, int]]) -> float:
    """Linear-interpolated quantile over a (value, count) distribution —
    SQL `percentile_cont` semantics: position `q * (total - 1)` on the rank
    axis, interpolating between the adjacent ranks when it falls between two
    distinct values. Empty -> 0.0."""
    if not vals:
        return 0.0
    total = sum(c for _, c in vals)
    if total <= 0:
        return 0.0
    pos = q * (total - 1)
    acc = 0
    prev_v: float | None = None
    for v, c in vals:
        last_rank = acc + c - 1
        if pos <= last_rank:
            if pos < acc and prev_v is not None:
                frac = pos - (acc - 1)
                return prev_v + (v - prev_v) * frac
            return v
        prev_v = v
        acc = last_rank + 1
    return vals[-1][0]


def _fetch_projected_slice(
    *,
    slice_: LokiReadSlice,
    pipeline: str,
    start_ns: int,
    end_ns: int,
    limit_per_slice: int,
    out: list[tuple[int, int | None, str]],
) -> None:
    """Append one projected stream slice, bisecting a full Loki response."""

    def fetch(s0_ns: int, e0_ns: int, depth: int = 0) -> None:
        """Fetch [s0_ns, e0_ns) backward; a full page is bisected."""
        params = {
            "query": pipeline,
            "limit": limit_per_slice,
            "direction": "backward",
            "start": s0_ns,
            "end": e0_ns,
        }
        payload = _get_json(
            settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range",
            params,
            endpoint="query_range",
        )
        rows: list[tuple[int, int | None, str]] = []
        for stream in payload.get("data", {}).get("result", []):
            agent_label = "agent_id" if slice_.era is LokiReadEra.INDEXED else "_projected_agent_id"
            stream_labels = stream.get("stream", {})
            aid_raw = stream_labels.get(agent_label, stream_labels.get("agent_id", ""))
            aid = int(aid_raw) if aid_raw else None
            for ts_ns, line in stream.get("values", []):
                rows.append((int(ts_ns), aid, line))
        rows.sort(key=lambda r: r[0], reverse=True)
        if len(rows) >= limit_per_slice:
            if depth >= 8 or e0_ns - s0_ns <= 1_000_000_000:
                raise RuntimeError(
                    f"query_projected_lines: slice [{s0_ns}, {e0_ns}) still "
                    f"holds >= {limit_per_slice} rows at the bisection floor"
                )
            mid = (s0_ns + e0_ns) // 2
            fetch(s0_ns, mid, depth + 1)
            fetch(mid, e0_ns, depth + 1)
            return
        out.extend(rows)

    fetch(start_ns, end_ns)


def query_projected_lines(
    *,
    fields: list[str],
    template: str,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit_per_slice: int = 5000,
) -> list[tuple[int, int | None, str]]:
    """Projected row fetch — (ts_ns, agent_id, line) ascending, for client-side
    reduction of payload fields too wide to ship whole (metrics A3).

    Each `fields` entry becomes `| json <f>="attributes.<f>"` (comma-joined,
    one stage); `template` is the `| line_format` body referencing those
    labels (e.g. `"{{ len .body }}"`). Rows are fetched in time slices of
    `limit_per_slice` (Loki caps per-query limit); a slice that returns the
    full limit is bisected until it fits, and a slice still overflowing at
    the bisection floor (1s / depth 8) raises instead of silently dropping
    rows. agent_id comes from the stream
    labels ("" when absent). Duplicate boundary rows across slices are
    dropped; the result is sorted by ts_ns ascending.
    """
    window = _window(from_, to)
    if window is None:
        return []
    # _agg_pipeline (not _build_logql): the plain `| json` stage would
    # flatten every line into its own series (trace_id/span_id per line) and
    # the row fetch would crawl — same reason attribute_aggregate avoids it.
    pipelines = _agg_pipelines(
        window,
        agent_id=agent_id,
        exclude_agent_ids=exclude_agent_ids,
        service_only=service_only,
        event_names=event_names,
        level_min=level_min,
        level=level,
        grep=grep,
        categories=categories,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
    )
    start, end = window
    start_ns = int(start.timestamp() * 1e9)
    end_ns = int(end.timestamp() * 1e9)

    out: list[tuple[int, int | None, str]] = []

    # Estimate slice count from an exact count (same filter set), split the
    # window evenly; a slice that returns the full limit is bisected.
    try:
        n = count_events(
            agent_id=agent_id,
            exclude_agent_ids=exclude_agent_ids,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            machine=machine,
            trace_id=trace_id,
            attribute_filters=attribute_filters,
            from_=datetime.fromtimestamp(start_ns / 1e9, UTC),
            to=datetime.fromtimestamp(end_ns / 1e9, UTC),
        )
    except Exception:
        n = 0
    n_slices = max(1, min(256, (n + limit_per_slice - 1) // limit_per_slice))
    for slice_, base_pipeline in pipelines:
        pipeline = base_pipeline
        if slice_.era is LokiReadEra.LEGACY:
            # Legacy streams expose agent_id only inside the JSON body. Extract
            # it under a temporary label before line_format replaces that body.
            pipeline += ' | json _projected_agent_id="agent_id"'
        if fields:
            json_stage = ", ".join(
                f'{_escape_label(f)}="attributes.{_escape_label(f)}"' for f in fields
            )
            pipeline += f" | json {json_stage}"
        pipeline += f' | line_format "{template}"'
        slice_start_ns = int(slice_.start.timestamp() * 1e9)
        slice_end_ns = int(slice_.end.timestamp() * 1e9)
        # Even row-based split (stable when density is uniform: one pass).
        for i in range(n_slices):
            s = slice_start_ns + (slice_end_ns - slice_start_ns) * i // n_slices
            e = slice_start_ns + (slice_end_ns - slice_start_ns) * (i + 1) // n_slices
            if i == n_slices - 1:
                e = slice_end_ns
            _fetch_projected_slice(
                slice_=slice_,
                pipeline=pipeline,
                start_ns=s,
                end_ns=e,
                limit_per_slice=limit_per_slice,
                out=out,
            )
    # Dedup (slice boundaries can repeat), window filter, ascending sort.
    seen: set[tuple[int, str]] = set()
    dedup: list[tuple[int, int | None, str]] = []
    for ts_ns, aid, line in out:
        if start_ns <= ts_ns <= end_ns and (ts_ns, line) not in seen:
            seen.add((ts_ns, line))
            dedup.append((ts_ns, aid, line))
    dedup.sort(key=lambda r: r[0])
    return dedup


def count_grouped(
    *,
    group_by: str,
    from_attributes: bool = False,
    exclude_empty: bool = False,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> dict[str, int]:
    """Count event lines grouped by one key — `sum by (X) (count_over_time)`
    as an instant query at the window end. `group_by` is a stream label
    (agent_id / event_name) or, with `from_attributes=True`, a nested payload
    key (extracted via one `| json` stage). Returns {key: count}; keys with
    count 0 never appear. `exclude_empty` drops the "" group (absent label)."""
    window = _window(from_, to)
    if window is None:
        return {}
    pipelines = _agg_pipelines(
        window,
        agent_id=agent_id,
        exclude_agent_ids=exclude_agent_ids,
        service_only=service_only,
        event_names=event_names,
        level_min=level_min,
        level=level,
        grep=grep,
        categories=categories,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
    )
    key = _escape_label(group_by)
    out: dict[str, int] = {}
    for slice_, base_pipeline in pipelines:
        pipeline = base_pipeline
        if from_attributes:
            pipeline += f' | json {key}="attributes.{key}"'
        logql = f"sum by ({key}) (count_over_time(({pipeline})[{_slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end):
            value = _result_value(series)
            if value is None:
                continue
            k = str(series.get("metric", {}).get(group_by, ""))
            if exclude_empty and k == "":
                continue
            out[k] = out.get(k, 0) + int(value)
    return out


def count_events_series(
    *,
    event_names: list[str],
    attribute_filters: dict[str, str] | None = None,
    group_by: str | None = None,
    from_attributes: bool = False,
    from_: datetime,
    to: datetime,
    step_s: int,
) -> dict[str, list[tuple[int, int]]]:
    """Per-step event counts over `[from_, to]` on a `step_s` grid —
    `sum by (k) (count_over_time((pipeline)[step_s]))` as a *range* query
    with `step=step_s` (the ops panel buckets by 60s/300s/1800s/3600s).

    Returns `{group: [(ts_s, count), ...]}`; the timestamps are the range
    query's evaluation points — bucket END times aligned to
    `from_ + i * step_s` — so a point at `t` counts the lines in `(t-step, t]`.
    Steps with no matching lines are absent; callers zero-fill against their
    own grid. `group_by` is a stream/structured-metadata key or, with
    `from_attributes=True`, a nested payload key (extracted via one
    `| json` stage). With `group_by=None` the single series is keyed `""`.
    """
    window = _window(from_, to)
    if window is None:
        return {}
    start, end = window
    key = _escape_label(group_by) if group_by else None
    step = max(1, step_s)
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"
    values_by_group: dict[str, dict[int, int]] = {"": {}} if group_by is None else {}
    for era, legacy_streams_only in _range_eras(window):
        pipeline = _agg_pipeline(
            era=era,
            legacy_streams_only=legacy_streams_only,
            event_names=event_names,
            attribute_filters=attribute_filters,
        )
        if group_by is not None and from_attributes:
            pipeline += f' | json {key}="attributes.{key}"'
        if key:
            logql = f"sum by ({key}) (count_over_time(({pipeline})[{step}s]))"
        else:
            logql = f"sum(count_over_time(({pipeline})[{step}s]))"
        payload = _get_json(
            url,
            {
                "query": logql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": f"{step}s",
            },
            endpoint="query_range",
        )
        for series in payload.get("data", {}).get("result", []):
            group = str(series.get("metric", {}).get(group_by, "")) if group_by else ""
            totals = values_by_group.setdefault(group, {})
            for ts, value in series.get("values", []):
                try:
                    ts_s = int(float(ts))
                    totals[ts_s] = totals.get(ts_s, 0) + int(float(value))
                except (TypeError, ValueError):
                    continue
    return {group: sorted(values.items()) for group, values in values_by_group.items()}


def attribute_max_series(
    *,
    field: str,
    event_names: list[str],
    attribute_filters: dict[str, str] | None = None,
    from_: datetime,
    to: datetime,
    step_s: int,
) -> list[tuple[int, float]]:
    """Per-step max of a numeric event attribute over `[from_, to]` on a
    `step_s` grid — `max(max_over_time((pipeline | json f="attributes.f" |
    unwrap f)[step_s]))` as a range query (the ops panel's exact per-bucket
    LLM latency max; Prometheus histograms only give bucket-bound
    approximations).

    Returns [(ts_s, max), ...] at the evaluation points (bucket END times,
    aligned to `from_ + i * step_s`); steps without samples are absent.
    """
    window = _window(from_, to)
    if window is None:
        return []
    start, end = window
    key = _escape_label(field)
    step = max(1, step_s)
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"
    maxima: dict[int, float] = {}
    for era, legacy_streams_only in _range_eras(window):
        pipeline = _agg_pipeline(
            era=era,
            legacy_streams_only=legacy_streams_only,
            event_names=event_names,
            attribute_filters=attribute_filters,
        )
        logql = f'max(max_over_time(({pipeline} | json {key}="attributes.{key}" | unwrap {key})[{step}s]))'
        payload = _get_json(
            url,
            {
                "query": logql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": f"{step}s",
            },
            endpoint="query_range",
        )
        for series in payload.get("data", {}).get("result", []):
            for ts, value in series.get("values", []):
                try:
                    ts_s = int(float(ts))
                    maxima[ts_s] = max(maxima.get(ts_s, float("-inf")), float(value))
                except (TypeError, ValueError):
                    continue
    return sorted(maxima.items())
