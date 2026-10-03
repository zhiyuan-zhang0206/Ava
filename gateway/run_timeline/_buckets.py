"""Time-bucketing of per-turn run-timeline rows: each bucket merges the turns that start in it."""

from __future__ import annotations

from datetime import datetime, timedelta

from gateway.run_timeline.schemas import RunTimelineLlm, RunTimelineRow


def bucket_rows(
    rows: list[RunTimelineRow], window_start: datetime, bucket_seconds: int
) -> list[RunTimelineRow]:
    groups: dict[int, list[RunTimelineRow]] = {}
    for row in rows:
        index = max(0, int((row.start - window_start).total_seconds() // bucket_seconds))
        groups.setdefault(index, []).append(row)

    return [
        _bucket_row(group, window_start + timedelta(seconds=index * bucket_seconds))
        for index, group in sorted(groups.items())
    ]


def _merged_llm(llm_events: list[RunTimelineLlm]) -> RunTimelineLlm:
    models = {llm.model for llm in llm_events if llm.model is not None}
    return RunTimelineLlm(
        calls=sum(llm.calls for llm in llm_events),
        in_total=sum(llm.in_total for llm in llm_events),
        cache_read=sum(llm.cache_read for llm in llm_events),
        out_total=sum(llm.out_total for llm in llm_events),
        reasoning=sum(llm.reasoning for llm in llm_events),
        latency_ms=sum(llm.latency_ms for llm in llm_events),
        cost_usd=sum(llm.cost_usd for llm in llm_events),
        model=next(iter(models)) if len(models) == 1 else "multiple" if models else None,
    )


def _bucket_row(group: list[RunTimelineRow], start: datetime) -> RunTimelineRow:
    """One time bucket's merged row."""
    return RunTimelineRow(
        turn=None,
        n_turns=sum(row.n_turns for row in group),
        start=start,
        end=max(row.end for row in group),
        active_s=sum(row.active_s for row in group),
        trace_id=None,
        checkpoint_id=None,
        ok=all(row.ok is True for row in group),
        llm=_merged_llm([row.llm for row in group]),
        execs=[exec_ for row in group for exec_ in row.execs],
        anomalies=sorted({anomaly for row in group for anomaly in row.anomalies}),
        tags=sorted({tag for row in group for tag in row.tags}),
    )
