"""Loki-side aggregate fetch for metrics — the gateway + CLI path (task #1197 A3).

The pre-aggregation design: the former per-row path materialized every
telemetry/log row in the window as an EventRow — 430K+ rows/day in prod,
measured +47MB RSS per /api/metrics call (memory:
ava/bugs/gateway-mem-profile-2026-08-03.md; audit finding F-s1-4). The metric
units reduced that stream to a handful of aggregates (counts, sums, pctiles,
per-key counters, position buckets); the SQL path fetched those aggregates
in Postgres. With the telemetry stack cutover (task #1197) `fetch_aggregate`
now runs the same reductions over Loki via `gateway.loki_events` — injected
as the `loki` argument (shared code never imports gateway; tests pass a
fake). `fetch_aggregate` runs ~20 compact Loki queries (exact counts,
per-key grouped counts, projected payload rows for the distributions) and
returns an `EventAggregate`; `build_report_from_aggregate` /
`agent_rollups_from_aggregate` rebuild the report from it, reusing the same
helper functions (`pctiles`, `third_of`, `_fix_kinds`, `cost_usd`) so the
math cannot drift.

Semantics preserved from the retired SQL path; documented deltas:
- row ordering inside an agent: SQL broke ties by `id` (insertion order),
  Loki by nanosecond `ts` (the writer stamps every event; ties are
  practically impossible);
- `sdk_fns` counter ties now resolve deterministically (count desc, fn asc)
  instead of first-seen order;
- code/exec `len` counts bytes (SQL counted characters) — identical for
  ASCII, and the digest percentiles are insensitive to the difference.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from shared.lm.pricing import cost_usd
from shared.metrics import (
    MetricSection,
    _fix_kinds,
    _render_agent_activity,
    _render_exec,
    _render_llm_turns,
    _render_sdk_usage,
    _render_syntax_fix,
    pctiles,
    third_of,
)


@dataclass(frozen=True)
class _LlmTotals:
    """Token + cost totals over one set of llm_usage rows — call count, token
    totals, cost, unpriced count (the SQL aggregate's per-model sums)."""

    calls: int
    tin: int
    tout: int
    tcached: int
    treason: int
    cost: float
    unpriced: int


@dataclass
class _PerAgentAgg:
    """Per-agent row aggregates — turn counts, exec outcomes, event span.

    Includes the service-level group (agent_id None): the per-row units count
    it toward `turns_per_agent` and `agent_lifetime_s` (by_agent buckets every
    group), so the aggregate path must too; the /agents endpoint filters it.
    """

    agent_id: int | None
    events: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    first_ts: datetime
    last_ts: datetime


@dataclass
class EventAggregate:
    """The window reduced in SQL — compact input for the report assemblers.

    Field names mirror the per-row units' reductions 1:1 so the assemblers
    stay a mechanical transcription. Lists that only feed `pctiles` /
    position bucketing keep their values (they are ints/floats, not EventRow
    tuples); per-key counters keep their rows in stream order so Counter
    insertion order — and therefore `most_common` tie order — matches the
    per-row path exactly.
    """

    total_events: int
    distinct_agents: int
    # syntax_fix
    code_blocks: int
    code_blocks_per_agent: dict[int | None, int]
    fix_events: list[tuple[int | None, int, str]]  # (agent_id, block_no, fixes) — stream order
    # exec
    exec_ok: int
    exec_failed: int
    fail_rows: list[tuple[str, str | None]]  # (event_name, exc_type) — stream order
    code_len: list[float]
    output_len: list[float]
    # llm / turns
    llm_by_model: list[
        tuple[str, int, int, int, int, int]
    ]  # (model, calls, in, out, cached, reasoning)
    llm_position: list[
        tuple[int | None, int, int]
    ]  # (agent_id, cache_read, in_total) — stream order
    turn_total: int
    turn_ok: int
    turn_durations: list[float]
    per_agent: dict[int | None, _PerAgentAgg]
    per_agent_llm: dict[
        int | None, list[tuple[str, int, int, int, int]]
    ]  # (model, calls, in, out, cached)
    # agent activity
    spawners: list[str]  # stream order
    lifecycle: dict[str, int]
    idle_halts: int
    # sdk usage
    sdk_fns: list[str]  # stream order


_MAX_WORKERS = 4


class LokiBackend(Protocol):
    """The slice of `gateway.loki_events` `fetch_aggregate` needs — injected
    (shared code must not import gateway; tests pass a fake). Signatures
    mirror `gateway.loki_events` 1:1 so the module satisfies the protocol
    structurally (pyright checks call compatibility both ways)."""

    def count_events(
        self,
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
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> int: ...

    def count_grouped(
        self,
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
    ) -> dict[str, int]: ...

    def query_events(
        self,
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
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        direction: str = "backward",
    ) -> tuple[list[dict[str, Any]], bool]: ...

    def query_projected_lines(
        self,
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
    ) -> list[tuple[int, int | None, str]]: ...


# projected-line templates (\x1f separator: payload values never contain it)
_SEP = "\x1f"
_T_BODY_LEN = "{{ len .body }}"
_T_TURN_DUR = "{{.duration_seconds}}"
_T_LLM = _SEP.join(
    f"{{{{.{f}}}}}" for f in ("model", "in_total", "out_total", "cache_read", "reasoning")
)
_T_EVENT_EXC = "{{.event_name}}" + _SEP + "{{.exc_type}}"
_T_EVENT_FIXES = "{{.event_name}}" + _SEP + "{{.fixes}}"
_T_SPAWNER = "{{.spawner}}"
_T_BODY = "{{.body}}"

_EXEC_FAIL_NAMES = ["^exec_", "^exec\\("]  # mirrors _AGG_EXEC_FAIL: exec_* or exec(..., excl 'exec'
_LIFECYCLE_NAMES = ["agent_spawned", "agent_terminated", "agent_restarted", "agent_resurrected"]


def _aggregate_tasks(loki: LokiBackend) -> dict[str, Callable[[dict[str, Any]], Any]]:
    """One query task per aggregate — each takes one partition's filter kwargs."""
    return {
        "total": lambda p: loki.count_events(**p),
        "code_blocks": lambda p: loki.count_events(event_names=["code"], **p),
        "exec_ok": lambda p: loki.count_events(event_names=["exec"], **p),
        "turn_total": lambda p: loki.count_events(event_names=["turn_end"], **p),
        "turn_ok": lambda p: loki.count_events(
            event_names=["turn_end"], attribute_filters={"ok": "true"}, **p
        ),
        "idle_halts": lambda p: loki.count_events(
            event_names=["halt"], attribute_filters={"body": "no tool_call (idle)"}, **p
        ),
        "lifecycle": lambda p: loki.count_grouped(
            group_by="event_name", event_names=_LIFECYCLE_NAMES, **p
        ),
        "fail_rows": lambda p: loki.query_projected_lines(
            fields=["exc_type"], template=_T_EVENT_EXC, event_names=_EXEC_FAIL_NAMES, **p
        ),
        "code_len": lambda p: loki.query_projected_lines(
            fields=["body"], template=_T_BODY_LEN, event_names=["code"], **p
        ),
        "output_len": lambda p: loki.query_projected_lines(
            fields=["body"], template=_T_BODY_LEN, event_names=["exec", *_EXEC_FAIL_NAMES], **p
        ),
        "turn_dur": lambda p: loki.query_projected_lines(
            fields=["duration_seconds"], template=_T_TURN_DUR, event_names=["turn_end"], **p
        ),
        "llm": lambda p: loki.query_projected_lines(
            fields=["model", "in_total", "out_total", "cache_read", "reasoning"],
            template=_T_LLM,
            event_names=["llm_usage"],
            **p,
        ),
        "by_agent_all": lambda p: loki.count_grouped(group_by="agent_id", **p),
        "by_agent_code": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["code"], **p
        ),
        "by_agent_turn": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["turn_end"], **p
        ),
        "by_agent_turn_ok": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["turn_end"], attribute_filters={"ok": "true"}, **p
        ),
        "by_agent_exec": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["exec"], **p
        ),
        "by_agent_exec_fail": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=_EXEC_FAIL_NAMES, **p
        ),
        "spawners": lambda p: loki.query_projected_lines(
            fields=["spawner"], template=_T_SPAWNER, event_names=["agent_spawned"], **p
        ),
        "sdk_fns": lambda p: loki.count_grouped(
            group_by="fn", from_attributes=True, exclude_empty=True, event_names=["sdk_call"], **p
        ),
        "fix": lambda p: loki.query_projected_lines(
            fields=["fixes"],
            template=_T_EVENT_FIXES,
            event_names=["code", "syntax_fix"],
            **p,
        ),
    }


def _run_partitioned(
    parts: list[dict[str, Any]],
    tasks: dict[str, Callable[[dict[str, Any]], Any]],
) -> dict[str, list[Any]]:
    """Run every task over every partition in a bounded thread pool; Loki
    errors propagate (fail fast). Result lists are ordered by partition."""
    results: dict[str, list[Any]] = {t: [] for t in tasks}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futs = {
            (_pi, tname): ex.submit(fn, part)
            for _pi, part in enumerate(parts)
            for tname, fn in tasks.items()
        }
        for (_pi, tname), fut in futs.items():
            results[tname].append(fut.result())
    return results


def _merge_counts(vals: list[int]) -> int:
    return sum(vals)


def _merge_groups(vals: list[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in vals:  # partitions are disjoint by construction
        out.update(v)
    return out


def _merge_rows(vals: list[list[tuple[int, int | None, str]]]) -> list[tuple[int, int | None, str]]:
    return [r for v in vals for r in v]


def fetch_aggregate(
    days: int,
    agent_id: int | None,
    *,
    since_compact: bool = False,
    loki: LokiBackend,
) -> EventAggregate:
    """Windowed aggregate over the Loki event stream (task #1197 A3) — the
    gateway + CLI entry point. Same filters/semantics as the retired SQL path:
    category telemetry|log over [now-days, now], optional single-agent scope,
    optional since_compact (each agent's rows narrowed to at-or-after its
    latest compact halt; service rows always kept). `loki` is the
    `gateway.loki_events` module (injected: shared never imports gateway).
    """
    to = datetime.now(UTC)
    from_ = to - timedelta(days=days)
    base: dict[str, Any] = {"categories": ["telemetry", "log"], "from_": from_, "to": to}
    if agent_id is not None:
        base["agent_id"] = agent_id

    # since_compact: per-agent cutoff = latest compact halt in the window
    cutoffs: dict[int, datetime] = {}
    if since_compact:
        rows = loki.query_projected_lines(
            fields=["body"],
            template=_T_BODY,
            event_names=["halt"],
            grep="compact",
            **base,
        )
        for ts_ns, aid, _line in rows:  # rows ascending → last write = max
            if aid is not None:
                cutoffs[aid] = datetime.fromtimestamp(ts_ns / 1e9, UTC)

    # partitions: one per cutoff agent ([cutoff, to]) + the rest (excludes them)
    parts: list[dict[str, Any]] = []
    for aid, cutoff in cutoffs.items():
        parts.append(
            {"agent_id": aid, "from_": cutoff, "to": to, "categories": ["telemetry", "log"]}
        )
    rest = dict(base)
    if cutoffs:
        rest["exclude_agent_ids"] = sorted(cutoffs)
    parts.append(rest)

    tasks = _aggregate_tasks(loki)
    results = _run_partitioned(parts, tasks)
    counts = _assemble_counts(results)
    rows = _row_reductions(results)
    per_agent = _per_agent_rows(
        counts["by_agent_all"],
        counts["by_agent_turn"],
        counts["by_agent_turn_ok"],
        counts["by_agent_exec"],
        counts["by_agent_exec_fail"],
        from_=from_,
        to=to,
        cutoffs=cutoffs,
        loki=loki,
    )

    return EventAggregate(
        total_events=counts["total_events"],
        distinct_agents=counts["distinct_agents"],
        code_blocks=counts["code_blocks"],
        code_blocks_per_agent=counts["code_blocks_per_agent"],
        fix_events=rows["fix_events"],
        exec_ok=counts["exec_ok"],
        exec_failed=rows["exec_failed"],
        fail_rows=rows["fail_rows"],
        code_len=rows["code_len"],
        output_len=rows["output_len"],
        llm_by_model=rows["llm_by_model"],
        llm_position=rows["llm_position"],
        turn_total=counts["turn_total"],
        turn_ok=counts["turn_ok"],
        turn_durations=rows["turn_durations"],
        per_agent=per_agent,
        per_agent_llm=rows["per_agent_llm"],
        spawners=rows["spawners"],
        lifecycle=counts["lifecycle"],
        idle_halts=counts["idle_halts"],
        sdk_fns=counts["sdk_fns"],
    )


def _assemble_counts(results: dict[str, list[Any]]) -> dict[str, Any]:
    """Merge per-partition count/grouped results into flat scalars + dicts."""
    lifecycle = {
        {
            "agent_spawned": "spawned",
            "agent_terminated": "terminated",
            "agent_restarted": "restarted",
            "agent_resurrected": "resurrected",
        }[k]: n
        for k, n in _merge_groups(results["lifecycle"]).items()
    }
    by_agent_all = _merge_groups(results["by_agent_all"])
    by_agent_code = _merge_groups(results["by_agent_code"])
    by_agent_turn = _merge_groups(results["by_agent_turn"])
    by_agent_turn_ok = _merge_groups(results["by_agent_turn_ok"])
    by_agent_exec = _merge_groups(results["by_agent_exec"])
    by_agent_exec_fail = _merge_groups(results["by_agent_exec_fail"])

    def _key(aid: str) -> int | None:
        return int(aid) if aid else None

    return {
        "total_events": _merge_counts(results["total"]),
        "code_blocks": _merge_counts(results["code_blocks"]),
        "exec_ok": _merge_counts(results["exec_ok"]),
        "turn_total": _merge_counts(results["turn_total"]),
        "turn_ok": _merge_counts(results["turn_ok"]),
        "idle_halts": _merge_counts(results["idle_halts"]),
        "lifecycle": lifecycle,
        "by_agent_all": by_agent_all,
        "by_agent_code": by_agent_code,
        "by_agent_turn": by_agent_turn,
        "by_agent_turn_ok": by_agent_turn_ok,
        "by_agent_exec": by_agent_exec,
        "by_agent_exec_fail": by_agent_exec_fail,
        "distinct_agents": len([k for k in by_agent_all if k]),
        "code_blocks_per_agent": {_key(k): v for k, v in by_agent_code.items()},
        "sdk_fns": [
            fn
            for fn, cnt in sorted(
                _merge_groups(results["sdk_fns"]).items(), key=lambda kv: (-kv[1], kv[0])
            )
            for _ in range(cnt)
        ],
    }


def _row_reductions(results: dict[str, list[Any]]) -> dict[str, Any]:
    """Client-side reductions over projected rows (stream order preserved)."""

    def _sort_rows(rows: list[tuple[int, int | None, str]]) -> list[tuple[int, int | None, str]]:
        return sorted(rows, key=lambda r: (r[1] if r[1] is not None else 10**9, r[0]))

    def _num(line: str) -> float:
        try:
            return float(line)
        except ValueError:
            return 0.0

    fail_rows: list[tuple[str, str | None]] = []
    for _ts_ns, _aid, line in _sort_rows(_merge_rows(results["fail_rows"])):
        ev, sep, exc = line.partition("\x1f")
        fail_rows.append((ev, exc if sep else None))

    llm_rows = _sort_rows(_merge_rows(results["llm"]))
    llm_by_model: dict[str, list[int]] = {}  # model -> [calls, in, out, cached, reason]
    llm_position: list[tuple[int | None, int, int]] = []
    per_agent_llm: dict[int | None, list[tuple[str, int, int, int, int]]] = {}
    for _ts_ns, aid, line in llm_rows:
        parts_l = line.split("\x1f")
        if len(parts_l) != 5:
            continue
        model, i_s, o_s, c_s, r_s = parts_l
        try:
            tin, tout, tcached, treason = int(i_s or 0), int(o_s or 0), int(c_s or 0), int(r_s or 0)
        except ValueError:
            continue
        model = model or ""
        acc = llm_by_model.setdefault(model, [0, 0, 0, 0, 0])
        acc[0] += 1
        acc[1] += tin
        acc[2] += tout
        acc[3] += tcached
        acc[4] += treason
        llm_position.append((aid, tcached, tin))
        per_agent_llm.setdefault(aid, []).append((model, 1, tin, tout, tcached))

    # syntax_fix: running code-block index per agent (window-function replica)
    fix_events: list[tuple[int | None, int, str]] = []
    blk: dict[int | None, int] = {}
    for _ts_ns, aid, line in _sort_rows(_merge_rows(results["fix"])):
        ev, sep, fixes = line.partition("\x1f")
        if ev == "code":
            blk[aid] = blk.get(aid, 0) + 1
        elif ev == "syntax_fix":
            fix_events.append((aid, blk.get(aid, 0), fixes if sep else ""))

    return {
        "fail_rows": fail_rows,
        "exec_failed": len(fail_rows),
        "code_len": [_num(line) for _t, _a, line in _merge_rows(results["code_len"])],
        "output_len": [_num(line) for _t, _a, line in _merge_rows(results["output_len"])],
        "turn_durations": [_num(line) for _t, _a, line in _merge_rows(results["turn_dur"])],
        "llm_by_model": [
            (m, acc[0], acc[1], acc[2], acc[3], acc[4]) for m, acc in llm_by_model.items()
        ],
        "llm_position": llm_position,
        "per_agent_llm": per_agent_llm,
        "spawners": [
            (line if line else "?") for _t, _a, line in _sort_rows(_merge_rows(results["spawners"]))
        ],
        "fix_events": fix_events,
    }


def _per_agent_rows(
    by_agent_all: dict[str, int],
    by_agent_turn: dict[str, int],
    by_agent_turn_ok: dict[str, int],
    by_agent_exec: dict[str, int],
    by_agent_exec_fail: dict[str, int],
    *,
    from_: datetime,
    to: datetime,
    cutoffs: dict[int, datetime],
    loki: LokiBackend,
) -> dict[int | None, _PerAgentAgg]:
    """Per-agent aggregates; first/last ts are fetched only for agents with
    >= 2 events (the only rows `agent_lifetime_s` uses)."""

    def _key(aid: str) -> int | None:
        return int(aid) if aid else None

    per_agent: dict[int | None, _PerAgentAgg] = {}
    for aid_str, events_n in by_agent_all.items():
        aid = _key(aid_str)
        per_agent[aid] = _PerAgentAgg(
            aid,
            events_n,
            by_agent_turn.get(aid_str, 0),
            by_agent_turn_ok.get(aid_str, 0),
            by_agent_exec.get(aid_str, 0),
            by_agent_exec_fail.get(aid_str, 0),
            from_,
            from_,  # placeholder; spans only use events>=2 rows
        )

    def first_last(aid: int | None) -> tuple[datetime, datetime]:
        kw: dict[str, Any] = {
            "categories": ["telemetry", "log"],
            "from_": from_,
            "to": to,
            "limit": 1,
        }
        if aid is None:
            kw["service_only"] = True
        else:
            kw["agent_id"] = aid
            if aid in cutoffs:
                kw["from_"] = cutoffs[aid]
        last_rows, _ = loki.query_events(direction="backward", **kw)
        first_rows, _ = loki.query_events(direction="forward", **kw)

        def _ts(rows: list[dict[str, Any]]) -> datetime:
            return rows[0]["ts"] if rows else from_

        return _ts(first_rows), _ts(last_rows)

    need = [aid for aid, row in per_agent.items() if row.events >= 2]
    if need:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futs = {aid: ex.submit(first_last, aid) for aid in need}
            for aid, fut in futs.items():
                first, last = fut.result()
                row = per_agent[aid]
                per_agent[aid] = _PerAgentAgg(
                    row.agent_id,
                    row.events,
                    row.turn_total,
                    row.turn_ok,
                    row.exec_ok,
                    row.exec_failed,
                    first,
                    last,
                )
    return per_agent


def _llm_totals_from_models(rows: list[tuple[str, int, int, int, int, int]]) -> _LlmTotals:
    """Token + cost totals from per-model sums — cost_usd is linear in tokens
    per model, so pricing the summed tokens once per model equals the per-row
    loop (`_llm_totals`) exactly; every call on an unpriced model is unpriced."""
    calls = sum(r[1] for r in rows)
    tin = sum(r[2] for r in rows)
    tout = sum(r[3] for r in rows)
    tcached = sum(r[4] for r in rows)
    treason = sum(r[5] for r in rows)
    cost = 0.0
    unpriced = 0
    for model, n, i, o, c, _r in rows:
        price = cost_usd(model, i, o, c)
        if price is None:
            unpriced += n
        else:
            cost += price
    return _LlmTotals(calls, tin, tout, tcached, treason, cost, unpriced)


def _sections_from_aggregate(agg: EventAggregate) -> list[MetricSection]:
    """Rebuild the five unit data dicts from `EventAggregate`, then render
    them with the shared render functions — the text blocks stay identical to
    the per-row path because the renders are the same functions."""
    # syntax_fix: per-event kinds (Counter, stream order) + per-block
    # ruff_format position thirds (block index i among the agent's n blocks ->
    # third_of(i, n)).
    kind_counts: Counter[str] = Counter()
    hits: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    blocks_b: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    for _agent_id, blk, fixes in agg.fix_events:
        if blk <= 0:
            continue
        kinds = set(_fix_kinds(fixes))
        kind_counts.update(kinds)
        b = third_of(blk - 1, agg.code_blocks_per_agent.get(_agent_id, 0))
        if "ruff_format" in kinds:
            hits[b] += 1
    for _agent_id, n in agg.code_blocks_per_agent.items():
        for i in range(n):
            blocks_b[third_of(i, n)] += 1
    rate = {
        b: round(hits[b] / blocks_b[b] * 100, 1) if blocks_b[b] else 0.0
        for b in ("early", "mid", "late")
    }
    data_syntax = {
        "code_blocks": agg.code_blocks,
        "trigger_counts": dict(kind_counts),
        "ruff_format_by_position": {
            b: {"hits": hits[b], "blocks": blocks_b[b]} for b in ("early", "mid", "late")
        },
        "ruff_format_rate_pct_by_position": rate,
    }

    # exec
    total_exec = agg.exec_ok + agg.exec_failed
    # The per-row unit extends fail rows event-bucket by event-bucket (by_event
    # dict order = event first-appearance in the stream), so the Counter's
    # insertion order — and most_common's tie order — is event-major, not raw
    # stream order. Stable-sort the stream-ordered rows to reproduce it.
    first_seen: dict[str, int] = {}
    for _i, (event_name, _exc) in enumerate(agg.fail_rows):
        first_seen.setdefault(event_name, _i)
    fail_rows = sorted(agg.fail_rows, key=lambda r: first_seen[r[0]])
    exc_types: Counter[str] = Counter()
    for event_name, exc_type in fail_rows:
        exc_types[exc_type or event_name] += 1
    data_exec = {
        "exec_total": total_exec,
        "exec_ok": agg.exec_ok,
        "exec_failed": agg.exec_failed,
        "success_rate_pct": round(agg.exec_ok / total_exec * 100, 1) if total_exec else 0.0,
        "failure_types": dict(exc_types),
        "code_len_chars": pctiles(agg.code_len),
        "output_len_chars": pctiles(agg.output_len),
    }

    # llm_turns
    totals = _llm_totals_from_models(agg.llm_by_model)
    # position thirds over per-agent llm_usage rows: (cache_read, in_total).
    pos: dict[str, list[int]] = {"early": [0, 0], "mid": [0, 0], "late": [0, 0]}
    by_agent: dict[int | None, list[tuple[int, int]]] = defaultdict(list)
    for aid, cache, tin in agg.llm_position:
        by_agent[aid].append((cache, tin))
    for rows in by_agent.values():
        n = len(rows)
        for i, (cache, tin) in enumerate(rows):
            bucket = pos[third_of(i, n)]
            bucket[0] += cache
            bucket[1] += tin
    pos_hit = {b: round(c / i * 100, 1) if i else 0.0 for b, (c, i) in pos.items()}
    turns_per_agent = [row.turn_total for row in agg.per_agent.values()]
    data_llm = {
        "llm_calls": totals.calls,
        "tokens_in": totals.tin,
        "tokens_out": totals.tout,
        "tokens_cached": totals.tcached,
        "tokens_reasoning": totals.treason,
        "cache_hit_pct": round(totals.tcached / totals.tin * 100, 1) if totals.tin else 0.0,
        "cache_hit_pct_by_position": pos_hit,
        "cost_usd": round(totals.cost, 4),
        "cost_unpriced_calls": totals.unpriced,
        "turn_duration_s": pctiles(agg.turn_durations),
        "turn_ok": agg.turn_ok,
        "turn_total": agg.turn_total,
        "turns_per_agent": pctiles([float(x) for x in turns_per_agent]),
    }

    # agent_activity
    by_spawner: Counter[str] = Counter()
    subagents = 0
    for sp in agg.spawners:
        by_spawner[sp.split(":", 1)[0]] += 1
        if sp.startswith("agent:"):
            subagents += 1
    spans = [
        (row.last_ts - row.first_ts).total_seconds()
        for row in agg.per_agent.values()
        if row.events >= 2
    ]
    data_activity = {
        "distinct_agents": agg.distinct_agents,
        "spawns_total": len(agg.spawners),
        "spawns_by_spawner": dict(by_spawner),
        "subagent_spawns": subagents,
        "lifecycle": {
            k: agg.lifecycle.get(k, 0)
            for k in ("spawned", "terminated", "restarted", "resurrected")
        },
        "idle_halts": agg.idle_halts,
        "agent_lifetime_s": pctiles(spans),
    }

    # sdk_usage
    func_counts: Counter[str] = Counter(agg.sdk_fns)
    ns_counts: Counter[str] = Counter()
    for fq, cnt in func_counts.items():
        ns_counts[fq.split(".", 1)[0]] += cnt
    data_sdk = {
        "code_blocks": agg.code_blocks,
        "total_calls": sum(func_counts.values()),
        "distinct_functions": len(func_counts),
        "functions": [{"function": f, "count": c} for f, c in func_counts.most_common()],
        "by_namespace": dict(ns_counts),
    }

    return [
        MetricSection("syntax_fix", _render_syntax_fix(data_syntax), data_syntax),
        MetricSection("exec", _render_exec(data_exec), data_exec),
        MetricSection("llm_turns", _render_llm_turns(data_llm), data_llm),
        MetricSection("agent_activity", _render_agent_activity(data_activity), data_activity),
        MetricSection("sdk_usage", _render_sdk_usage(data_sdk), data_sdk),
    ]


def build_report_from_aggregate(
    agg: EventAggregate,
    days: int,
    agent_id: int | None,
    *,
    since_compact: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Assemble the same text + data `build_report` produces, from an
    `EventAggregate` (see `fetch_aggregate`). Meta semantics are identical:
    `total_events` counts every telemetry/log row in the window (service-level
    rows included), `distinct_agents` excludes agent_id NULL."""
    sections = _sections_from_aggregate(agg)
    names = [s.name for s in sections]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate metric section names would clobber the JSON: {names}")
    generated_at = datetime.now(UTC).isoformat()
    meta: dict[str, Any] = {
        "window_days": days,
        "agent_filter": agent_id,
        "generated_at": generated_at,
        "total_events": agg.total_events,
        "distinct_agents": agg.distinct_agents,
        "since_compact": since_compact,
    }
    header = (
        "=" * 64
        + f"\n  Ava metrics — last {days}d"
        + (" — since last compact" if since_compact else "")
        + (f" — agent {agent_id}" if agent_id is not None else "")
        + f"\n  {agg.total_events} events / {agg.distinct_agents} agents"
        + f" / generated {generated_at[:19]}\n"
        + "=" * 64
        + "\n"
    )
    text = header + "\n" + "\n".join(s.text for s in sections)
    data = {"meta": meta, "metrics": {s.name: s.data for s in sections}}
    return text, data


def agent_rollups_from_aggregate(agg: EventAggregate) -> dict[int, dict[str, Any]]:
    """Per-agent headline counters for `/api/metrics/agents` — the same shape
    `agent_rollup` produces per agent, from the aggregate fetch."""
    out: dict[int, dict[str, Any]] = {}
    for aid, row in agg.per_agent.items():
        if aid is None:
            continue
        usage = agg.per_agent_llm.get(aid, [])
        calls = sum(r[1] for r in usage)
        tin = sum(r[2] for r in usage)
        tout = sum(r[3] for r in usage)
        tcached = sum(r[4] for r in usage)
        cost = 0.0
        for _m, _n, i, o, c in usage:
            price = cost_usd(_m, i, o, c)
            if price is not None:
                cost += price
        out[aid] = {
            "events": row.events,
            "cost_usd": round(cost, 4),
            "llm_calls": calls,
            "tokens_in": tin,
            "tokens_out": tout,
            "tokens_cached": tcached,
            "cache_hit_pct": round(tcached / tin * 100, 1) if tin else 0.0,
            "turn_ok": row.turn_ok,
            "turn_total": row.turn_total,
            "exec_ok": row.exec_ok,
            "exec_failed": row.exec_failed,
        }
    return out
