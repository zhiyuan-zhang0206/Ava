"""Loki aggregate, distribution, and event-series reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast, overload

import httpx

from gateway import _loki_logql, _loki_transport, loki_events_cache
from services.events_maintenance import resolution as _event_resolution
from shared.config import settings
from shared.loki_index_labels import LokiReadSlice


def _quantile_aggregate(
    *,
    pipelines: list[tuple[LokiReadSlice, str]],
    field: str,
    quantile: float,
    group_by: str | None,
    timeout_s: float | None,
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
        f' | json {_loki_logql._escape_label(group_by)}="attributes.{_loki_logql._escape_label(group_by)}"'
        if group_by
        else ""
    )
    extract = f'{group_stage} | json {_loki_logql._escape_label(field)}="attributes.{_loki_logql._escape_label(field)}" | __error__=""'
    dist: list[dict[str, Any]] = []
    for slice_, base_pipeline in pipelines:
        pipeline = base_pipeline
        duration_s = _loki_logql._slice_duration_s(slice_)
        if group_by:
            logql = (
                f"sum by ({_loki_logql._escape_label(group_by)}, {_loki_logql._escape_label(field)}) ("
                f"count_over_time(({pipeline}{extract})[{duration_s}s]))"
            )
        else:
            logql = (
                f"sum by ({_loki_logql._escape_label(field)}) ("
                f"count_over_time(({pipeline}{extract})[{duration_s}s]))"
            )
        try:
            dist.extend(_query_instant(logql, slice_.end, timeout_s=timeout_s))
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
                timeout_s=timeout_s,
            )
            _relabel_buckets(bucketed, field)
            dist.extend(bucketed)
    if group_by:
        groups: dict[str, list[tuple[float, int]]] = {}
        for series in dist:
            metric = series.get("metric", {})
            value = _loki_transport._result_value(series)
            if value is None:
                continue
            g = str(metric.get(group_by, ""))
            groups.setdefault(g, []).append((float(metric.get(field, 0)), int(value)))
        return [
            (g, _loki_logql._weighted_quantile(quantile, sorted(vals)))
            for g, vals in groups.items()
        ]
    vals: list[tuple[float, int]] = []
    for series in dist:
        metric = series.get("metric", {})
        value = _loki_transport._result_value(series)
        if value is not None:
            vals.append((float(metric.get(field, 0)), int(value)))
    return _loki_logql._weighted_quantile(quantile, sorted(vals))


def _count_attribute_slices(
    *,
    pipelines: list[tuple[LokiReadSlice, str]],
    group_by: str | None,
    timeout_s: float | None,
) -> float | list[tuple[str, float]]:
    """Count each cutover slice and add scalar or group totals."""

    group_stage = (
        f' | json {_loki_logql._escape_label(group_by)}="attributes.{_loki_logql._escape_label(group_by)}"'
        if group_by
        else ""
    )
    totals: dict[str, float] = {}
    scalar = 0.0
    for slice_, pipeline in pipelines:
        if group_by:
            logql = (
                f"sum by ({_loki_logql._escape_label(group_by)}) (count_over_time(({pipeline}{group_stage})"
                f"[{_loki_logql._slice_duration_s(slice_)}s]))"
            )
        else:
            logql = f"sum(count_over_time(({pipeline})[{_loki_logql._slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end, timeout_s=timeout_s):
            value = _loki_transport._result_value(series)
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
    timeout_s: float | None,
) -> float | list[tuple[str, float]]:
    """Merge sum/min/max payload aggregates across non-overlapping slices."""

    group_stage = (
        f' | json {_loki_logql._escape_label(group_by)}="attributes.{_loki_logql._escape_label(group_by)}"'
        if group_by
        else ""
    )
    totals: dict[str, float] = {}
    scalar: float | None = None
    for slice_, pipeline in pipelines:
        unwrap_pipe = (
            f'{pipeline}{group_stage} | json {_loki_logql._escape_label(field)}="attributes.{_loki_logql._escape_label(field)}" '
            f'| __error__="" | unwrap {_loki_logql._escape_label(field)}'
        )
        if group_by:
            logql = (
                f"{cross_op} by ({_loki_logql._escape_label(group_by)}) ("
                f"{range_op}(({unwrap_pipe})[{_loki_logql._slice_duration_s(slice_)}s]))"
            )
        else:
            logql = (
                f"{cross_op}({range_op}(({unwrap_pipe})[{_loki_logql._slice_duration_s(slice_)}s]))"
            )
        for series in _query_instant(logql, slice_.end, timeout_s=timeout_s):
            value = _loki_transport._result_value(series)
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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    timeout_s: float | None = None,
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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    timeout_s: float | None = None,
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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    timeout_s: float | None = None,
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

    Notes (verified against real Loki, tasks #1197 and #1515):
    - agent_id and event_name are extracted individually from the event body
      because mixed-batch structured metadata can disagree with it; other
      event filters match structured metadata directly. A plain `| json` must
      NOT be added here: it flattens the nested `attributes` object into
      per-line labels and would split the aggregation into per-line series;
    - the numeric field needs its own `| json f="attributes.f"` stage (one
      extraction per stage — multiple extractions are a parse error);
    - `unwrap` only parses inside range aggregations.
    """
    _loki_transport._read_gate()
    window = _loki_logql._window(from_, to)
    if window is None:
        return [] if group_by else 0.0
    cache_key = loki_events_cache.make_key(
        "attribute_aggregate",
        {
            "field": field,
            "agg": agg,
            "quantile": quantile,
            "group_by": group_by,
            "agent_id": agent_id,
            "service_only": service_only,
            "event_names": event_names,
            "level_min": level_min,
            "level": level,
            "grep": grep,
            "categories": categories,
            "cluster": cluster,
            "machine": machine,
            "trace_id": trace_id,
            "attribute_filters": attribute_filters,
        },
        *window,
    )
    cached = loki_events_cache.get(cache_key)
    if isinstance(cached, list):
        return list(cast(list[tuple[str, float]], cached))
    if cached is not None:
        return cast(float, cached)
    ops = {
        "sum": ("sum_over_time", "sum"),
        "min": ("min_over_time", "min"),
        "max": ("max_over_time", "max"),
    }
    if agg == "quantile" and quantile is None:
        raise ValueError("attribute_aggregate(agg='quantile') needs quantile")
    if agg not in {*ops, "quantile", "count"}:
        raise ValueError(f"attribute_aggregate: unknown agg {agg!r}")
    holder, is_leader = loki_events_cache.begin(cache_key)
    if is_leader:
        cached = loki_events_cache.get(cache_key)
        if isinstance(cached, list):
            cached_list = cast(list[tuple[str, float]], cached)
            loki_events_cache.finish(cache_key, holder, value=cached_list)
            return list(cached_list)
        if cached is not None:
            loki_events_cache.finish(cache_key, holder, value=cached)
            return cast(float, cached)
    else:
        holder.event.wait(loki_events_cache._INFLIGHT_WAIT_S)
        if holder.error is not None:
            raise holder.error
        inflight_value = cast(float | list[tuple[str, float]] | None, holder.value)
        if isinstance(inflight_value, list):
            return list(inflight_value)
        if inflight_value is not None:
            return inflight_value
    try:
        pipelines = _loki_logql._agg_pipelines(
            window,
            agent_id=agent_id,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            cluster=cluster,
            machine=machine,
            trace_id=trace_id,
            attribute_filters=attribute_filters,
        )

        if agg == "quantile":
            result = _quantile_aggregate(
                pipelines=pipelines,
                field=field,
                quantile=cast(float, quantile),
                group_by=group_by,
                timeout_s=timeout_s,
            )

        elif agg == "count":
            result = _count_attribute_slices(
                pipelines=pipelines,
                group_by=group_by,
                timeout_s=timeout_s,
            )
        else:
            range_op, cross_op = ops[agg]
            result = _numeric_attribute_slices(
                pipelines=pipelines,
                field=field,
                agg=agg,
                group_by=group_by,
                range_op=range_op,
                cross_op=cross_op,
                timeout_s=timeout_s,
            )
        stored_result = list(result) if isinstance(result, list) else result
        loki_events_cache.put(cache_key, stored_result)
    except BaseException as exc:
        if is_leader:
            loki_events_cache.finish(cache_key, holder, error=exc)
        raise
    if is_leader:
        loki_events_cache.finish(cache_key, holder, value=stored_result)
    return result


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
    window = _loki_logql._window(from_, to)
    if window is None:
        return {}
    pipelines = _loki_logql._agg_pipelines(
        window,
        agent_id=agent_id,
        event_names=event_names,
        categories=categories,
        attribute_filters=attribute_filters,
    )
    counts: dict[str, int] = {}
    for slice_, pipeline in pipelines:
        logql = f"sum by (event_name) (count_over_time(({pipeline})[{_loki_logql._slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end):
            value = _loki_transport._result_value(series)
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
    window = _loki_logql._window(from_, to)
    if window is None:
        return []
    pipelines = _loki_logql._agg_pipelines(
        window,
        agent_id=agent_id,
        event_names=event_names,
        categories=categories,
        attribute_filters=attribute_filters,
    )
    extract = f' | json {_loki_logql._escape_label(field)}="attributes.{_loki_logql._escape_label(field)}" | __error__=""'
    totals: dict[float, int] = {}
    for slice_, pipeline in pipelines:
        duration_s = _loki_logql._slice_duration_s(slice_)
        logql = f"sum by ({_loki_logql._escape_label(field)}) (count_over_time(({pipeline}{extract})[{duration_s}s]))"
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
            value = _loki_transport._result_value(series)
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
    group = f"{_loki_logql._escape_label(group_by)}, " if group_by else ""
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


def _query_instant(
    logql: str, at: datetime, *, timeout_s: float | None = None
) -> list[dict[str, Any]]:
    """One Loki instant query; returns the result vectors (metric + value)."""
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query"
    params = {"query": logql, "time": at.timestamp()}
    payload = _loki_transport._get_json(url, params, endpoint="query", timeout_s=timeout_s)
    return payload.get("data", {}).get("result", [])


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
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    timeout_s: float | None = None,
) -> dict[str, int]:
    """Count event lines grouped by one key — `sum by (X) (count_over_time)`
    as an instant query at the window end. `group_by` is a stream label
    (agent_id / event_name) or, with `from_attributes=True`, a nested payload
    key (extracted via one `| json` stage). Returns {key: count}; keys with
    count 0 never appear. `exclude_empty` drops the "" group (absent label)."""
    window = _loki_logql._window(from_, to)
    if window is None:
        return {}
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
        cluster=cluster,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
    )
    key = _loki_logql._escape_label(group_by)
    out: dict[str, int] = {}
    for slice_, base_pipeline in pipelines:
        pipeline = base_pipeline
        if from_attributes:
            pipeline += f' | json {key}="attributes.{key}"'
        logql = f"sum by ({key}) (count_over_time(({pipeline})[{_loki_logql._slice_duration_s(slice_)}s]))"
        for series in _query_instant(logql, slice_.end, timeout_s=timeout_s):
            value = _loki_transport._result_value(series)
            if value is None:
                continue
            k = str(series.get("metric", {}).get(group_by, ""))
            if exclude_empty and k == "":
                continue
            out[k] = out.get(k, 0) + int(value)
    return out


def count_event_classes(
    *,
    from_: datetime,
    to: datetime,
    cluster: str | None = None,
    timeout_s: float | None = None,
) -> dict[_event_resolution.EventClass, int]:
    """Per-class warning/error counts over ``[from_, to]`` — the input to the
    class-resolution arithmetic (task #1935).

    Runs the events-maintenance daemon's grouped query
    (``services.events_maintenance.resolution.grouped_count_query``) through
    the gateway's own Loki budget, so the dashboard's three-way split
    (total / dismissed / net) subtracts exactly the classes the daemon's
    gauges subtract — one query shape, two windows. ``cluster`` scopes the
    raw counts to the current home like every other dashboard Loki read
    (unlabeled pre-labeling rows stay accepted); the dismissal set itself is
    global, so the cancellation matches the daemon either way. Keys are
    ``_event_resolution.EventClass`` values; ``critical`` rows carry their own level
    and fold into the error bucket in the arithmetic, not here.

    Transport and budget failures surface exactly like every other gateway
    Loki read (httpx.HTTPError / LokiQueryBudgetError -> 503 by the router).
    """
    window_s = int((to - from_).total_seconds())
    logql = _event_resolution.grouped_count_query(f"{window_s}s", cluster=cluster)
    counts: dict[_event_resolution.EventClass, int] = {}
    for series in _query_instant(logql, to, timeout_s=timeout_s):
        value = _loki_transport._result_value(series)
        if value is None:
            continue
        metric = series.get("metric", {})
        event_class = _event_resolution.EventClass(
            category=str(metric.get("category", "")),
            level=str(metric.get("level", "")),
            event_name=str(metric.get("event_name", "")),
            source=str(metric.get("source", "")),
        )
        counts[event_class] = counts.get(event_class, 0) + int(value)
    return counts


def count_events_series(
    *,
    event_names: list[str],
    cluster: str | None = None,
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
    _loki_transport._read_gate()
    window = _loki_logql._window(from_, to)
    if window is None:
        return {}
    start, end = window
    key = _loki_logql._escape_label(group_by) if group_by else None
    step = max(1, step_s)
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"
    values_by_group: dict[str, dict[int, int]] = {"": {}} if group_by is None else {}
    for era, legacy_unlabeled, indexed_labeled in _loki_logql._range_eras(window):
        pipeline = _loki_logql._agg_pipeline(
            era=era,
            legacy_unlabeled=legacy_unlabeled,
            indexed_labeled=indexed_labeled,
            event_names=event_names,
            cluster=cluster,
            attribute_filters=attribute_filters,
        )
        if group_by is not None and from_attributes:
            pipeline += f' | json {key}="attributes.{key}"'
        if key:
            logql = f"sum by ({key}) (count_over_time(({pipeline})[{step}s]))"
        else:
            logql = f"sum(count_over_time(({pipeline})[{step}s]))"
        payload = _loki_transport._get_json(
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
    window = _loki_logql._window(from_, to)
    if window is None:
        return []
    start, end = window
    key = _loki_logql._escape_label(field)
    step = max(1, step_s)
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"
    maxima: dict[int, float] = {}
    for era, legacy_unlabeled, indexed_labeled in _loki_logql._range_eras(window):
        pipeline = _loki_logql._agg_pipeline(
            era=era,
            legacy_unlabeled=legacy_unlabeled,
            indexed_labeled=indexed_labeled,
            event_names=event_names,
            attribute_filters=attribute_filters,
        )
        logql = f'max(max_over_time(({pipeline} | json {key}="attributes.{key}" | unwrap {key})[{step}s]))'
        payload = _loki_transport._get_json(
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
