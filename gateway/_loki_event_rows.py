"""Raw Loki event-row reads, counts, and line projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from gateway import _loki_logql, _loki_transport, loki_events_cache
from shared import telemetry
from shared.config import settings
from shared.events.contract import EventTier
from shared.loki_index_labels import LokiReadEra, LokiReadSlice

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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    archive: bool = False,
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

    With ``archive=True`` the rows come from the task #1281 archive stream
    (all pre-cutover events, one era — the live stream's index-label slices
    do not apply). Callers bound ``from_``/``to`` to the archive's span
    (ARCHIVE_FLOOR_AT..ARCHIVE_FREEZE_AT) to stay under Loki's 90d
    max_query_length.
    """
    _loki_transport._read_gate()
    window = _loki_logql._window(from_, to)
    if window is None:
        return [], False
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"

    if archive:
        # The archive stream is one era (no index-label cutover inside it).
        slices = (LokiReadSlice(LokiReadEra.LEGACY, window[0], window[1]),)
    else:
        slices = _loki_logql._read_slices(window)
    raw: list[tuple[int, str]] = []
    for slice_ in slices:
        logql = _loki_logql._build_logql(
            era=slice_.era,
            archive=archive,
            indexed_labeled=not archive and len(slices) == 2 and slice_.era is LokiReadEra.INDEXED,
            agent_id=agent_id,
            exclude_agent_ids=exclude_agent_ids,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            tiers=tiers,
            cluster=cluster,
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
        payload = _loki_transport._get_json(
            url, params, endpoint="query_range", timeout_s=timeout_s
        )
        for stream in payload.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                raw.append((int(ts_ns), line))
    # query_range groups by stream; each stream is already direction-sorted,
    # but cross-stream ordering needs one merge pass.
    raw.sort(key=lambda pair: pair[0], reverse=(direction == "backward"))

    # The exact-cutover row can arrive from both queries; (ts, line) is the
    # cross-cutover dedupe backstop after the time partition.
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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    timeout_s: float | None = None,
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
    _loki_transport._read_gate()
    window = _loki_logql._window(from_, to)
    if window is None:
        return 0
    cache_key = loki_events_cache.make_key(
        "count_events",
        {
            "agent_id": agent_id,
            "exclude_agent_ids": exclude_agent_ids,
            "service_only": service_only,
            "event_names": event_names,
            "level_min": level_min,
            "level": level,
            "grep": grep,
            "categories": categories,
            "tiers": tiers,
            "cluster": cluster,
            "machine": machine,
            "trace_id": trace_id,
            "attribute_filters": attribute_filters,
        },
        *window,
    )
    cached = loki_events_cache.get(cache_key)
    if cached is not None:
        return cast(int, cached)
    holder, is_leader = loki_events_cache.begin(cache_key)
    if is_leader:
        cached = loki_events_cache.get(cache_key)
        if cached is not None:
            loki_events_cache.finish(cache_key, holder, value=cached)
            return cast(int, cached)
    else:
        holder.event.wait(loki_events_cache._INFLIGHT_WAIT_S)
        if holder.error is not None:
            raise holder.error
        if holder.value is not None:
            return cast(int, holder.value)
    try:
        url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query"
        slices = _loki_logql._read_slices(window)
        total = 0
        for slice_ in slices:
            pipeline = _loki_logql._build_logql(
                era=slice_.era,
                indexed_labeled=len(slices) == 2 and slice_.era is LokiReadEra.INDEXED,
                agent_id=agent_id,
                exclude_agent_ids=exclude_agent_ids,
                service_only=service_only,
                event_names=event_names,
                level_min=level_min,
                level=level,
                grep=grep,
                categories=categories,
                tiers=tiers,
                cluster=cluster,
                machine=machine,
                trace_id=trace_id,
                attribute_filters=attribute_filters,
                drop_json_errors=True,
            )
            logql = f"sum(count_over_time(({pipeline})[{_loki_logql._slice_duration_s(slice_)}s]))"
            payload = _loki_transport._get_json(
                url,
                {"query": logql, "time": slice_.end.timestamp()},
                endpoint="query",
                timeout_s=timeout_s,
            )
            result = payload.get("data", {}).get("result", [])
            if result:
                value = result[0].get("value") or result[0].get("values", [None])[-1]
                total += int(value[1]) if value else 0
        loki_events_cache.put(cache_key, total)
    except BaseException as exc:
        if is_leader:
            loki_events_cache.finish(cache_key, holder, error=exc)
        raise
    if is_leader:
        loki_events_cache.finish(cache_key, holder, value=total)
    return total


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
        "start": from_.timestamp(),
        "end": to.timestamp(),
        "step": str(step_s),
        "limit": "2000",
    }
    payload = _loki_transport._get_json(url, params, endpoint="query_range")
    result = payload.get("data", {}).get("result", [])
    if not result:
        return []
    return [
        (datetime.fromtimestamp(int(float(ts_s)), UTC).isoformat(), float(v))
        for ts_s, v in result[0].get("values", [])
    ]


def _fetch_projected_slice(
    *,
    slice_: LokiReadSlice,
    pipeline: str,
    start_ns: int,
    end_ns: int,
    limit_per_slice: int,
    timeout_s: float | None,
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
        payload = _loki_transport._get_json(
            settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range",
            params,
            endpoint="query_range",
            timeout_s=timeout_s,
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
    timeout_s: float | None = None,
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
    dropped; the result is sorted by ts_ns ascending. `timeout_s` bounds both
    the count estimate and each projected-range request, so an interactive
    reduction cannot occupy a Loki-budget slot for the default client timeout.
    """
    window = _loki_logql._window(from_, to)
    if window is None:
        return []
    # _agg_pipeline (not _build_logql): the plain `| json` stage would
    # flatten every line into its own series (trace_id/span_id per line) and
    # the row fetch would crawl — same reason attribute_aggregate avoids it.
    pipelines = _loki_logql._agg_pipelines(
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
            timeout_s=timeout_s,
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
                f'{_loki_logql._escape_label(f)}="attributes.{_loki_logql._escape_label(f)}"'
                for f in fields
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
                timeout_s=timeout_s,
                out=out,
            )
    # Dedup cross-cutover and row-slice boundaries by (ts, line), then filter
    # the requested window and sort ascending.
    seen: set[tuple[int, str]] = set()
    dedup: list[tuple[int, int | None, str]] = []
    for ts_ns, aid, line in out:
        if start_ns <= ts_ns <= end_ns and (ts_ns, line) not in seen:
            seen.add((ts_ns, line))
            dedup.append((ts_ns, aid, line))
    dedup.sort(key=lambda r: r[0])
    return dedup
