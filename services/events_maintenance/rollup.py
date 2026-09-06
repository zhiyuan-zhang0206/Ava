"""Incremental Since-Birth rollup — day-grain aggregates sourced from Loki.

The unified event stream lives in Loki since the LGTM cutover (the PG
`events` copy is a frozen archive; its last-ever code read was the
llm-cost-rollup-columns migration backfill). Each maintenance pass rolls
whole UTC days up to yesterday into:

- ``agent_model_tokens_daily`` — per (agent, day, model): calls, token sums,
  and the cost ledger columns (cost_usd = summed usage-time price snapshots,
  costed_calls / unpriced_calls). Money is summed at usage-time rates, never
  re-priced.
- ``agent_metrics_daily`` — per (agent, day): turn totals/ok/durations and
  the exec ok/failed split.

Each retained day carries a dirty watermark in ``rollup_day_state``: one cheap
union-family count probe replaces the fourteen full aggregate queries for a
clean day. Missing, failed, count-changed, and recent late-write-window days get
the same idempotent **full-day overwrite recompute** keyed on the PK. A pass
deadline bounds how many day probes/recomputes one hourly run may start.

An indexed slice that returns zero aggregate rows is unsafe rather than
authoritatively empty: the pass warns and skips that day, preserving its
existing ledger rows while continuing with the remaining days.

Loki bounds what is recoverable: retention is 84h
(deploy/lgtm/config/loki.yaml `retention_period: 84h`), so the
recompute window clamps to the first FULLY-retained day — a maintenance
outage longer than retention leaves a gap in the Loki-sourced aggregates
(logged loudly; the filtered rollup-source JSONL mirror is the automated replay
source). The clamp also
protects history: a day at/below the floor is never recomputed, so
archive-backfilled rows cannot be overwritten with zeros.

Day boundary is UTC midnight; each day aggregates as Loki instant queries
evaluated at day end over a 24h range vector (Loki's (start, end] vs the
old SQL's [start, end) differ only at the exact midnight nanosecond).

Test seams: ``_day_source_count`` probes dirtiness and ``_day_aggregates`` does
the full reroll. Tests monkeypatch both and drive the watermark/clamp/upsert
logic against a real throwaway Postgres; LogQL builders have string-shape tests.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time

import psycopg
from psycopg import sql

from shared.log import logger
from shared.loki_index_labels import (
    EVENT_STREAM_RETENTION,
    LokiReadEra,
    escape_logql_label,
    event_stream_selector,
    split_index_label_window,
)
from shared.loki_query_budget import FairQueryBudget

_HTTP_TIMEOUT_S = 60.0
_query_budget = FairQueryBudget(capacity=1, max_waiters=4, wait_timeout_s=30.0)


@dataclass(frozen=True)
class TokensRow:
    """One (agent, model) group of one day's llm_usage aggregation."""

    agent_id: int
    model: str
    calls: int
    costed_calls: int
    unpriced_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float


@dataclass(frozen=True)
class MetricsRow:
    """One agent's turn/exec aggregates for one day."""

    agent_id: int
    turn_total: int
    turn_ok: int
    turn_dur_sum: float
    turn_dur_min: float | None
    turn_dur_max: float | None
    turn_dur_hist: dict[int, int]
    exec_ok: int
    exec_failed: int


@dataclass(frozen=True)
class RollupResult:
    """What one `compute_rollup` run attempted. `start_day`/`end_day` are the
    inclusive UTC-day range (None when no dirty day was attempted); `*_rows`
    are the successful upsert row counts."""

    start_day: date | None
    end_day: date | None
    metrics_rows: int
    tokens_rows: int


@dataclass(frozen=True)
class _RollupDayState:
    status: str
    source_count: int


_DayRollup = tuple[date, list[TokensRow], list[MetricsRow]]
_StateUpdate = tuple[date, str, int, str | None]


# ── LogQL builders ───────────────────────────────────────────────────────────

_DAY_S = 86400
_ROLLUP_EVENT_NAMES = ["llm_usage", "turn_end", "exec", "exec_.+", "exec\\(.*"]


def _event_pipeline(
    *,
    era: LokiReadEra,
    event_names: list[str],
    telemetry_only: bool,
    indexed_labeled: bool = False,
) -> str:
    """One rollup event family, selective only after resource-label cutover."""

    selector = event_stream_selector(
        era=era,
        agent_id=None,
        event_names=event_names,
        indexed_labeled=indexed_labeled,
    )
    parts = [
        selector,
        '| json agent_id_extracted="agent_id"',
        '| agent_id_extracted=~".+"',
    ]
    if telemetry_only:
        parts.append('| category=~"telemetry|log"')
    joined = "|".join(escape_logql_label(event_name) for event_name in event_names)
    parts.append('| json event_name_extracted="event_name"')
    parts.append(f'| event_name_extracted=~"{joined}"')
    return " ".join(parts)


def _tokens_queries(
    *,
    era: LokiReadEra = LokiReadEra.LEGACY,
    duration_s: int = _DAY_S,
    indexed_labeled: bool = False,
) -> dict[str, str]:
    """The per-(agent, model) instant queries for one day's tokens/cost row.

    Body-truth agent_id / event_name and payload fields each need their own
    single-extraction `| json` stage (multiple extractions in one stage are a
    parse error); category filters structured metadata directly."""
    llm = _event_pipeline(
        era=era,
        event_names=["llm_usage"],
        telemetry_only=True,
        indexed_labeled=indexed_labeled,
    )
    model = ' | json model="attributes.model"'
    out = {
        "calls": f"sum by (agent_id, model) (count_over_time(({llm}{model})[{duration_s}s]))",
        "costed_calls": (
            f"sum by (agent_id, model) (count_over_time(({llm}{model}"
            f' | json cost_usd="attributes.cost_usd" | cost_usd!="")[{duration_s}s]))'
        ),
    }
    for name, field in (
        ("tokens_in", "in_total"),
        ("tokens_out", "out_total"),
        ("tokens_cached", "cache_read"),
        ("tokens_reasoning", "reasoning"),
        ("cost_usd", "cost_usd"),
    ):
        out[name] = (
            f"sum by (agent_id, model) (sum_over_time(({llm}{model}"
            f' | json {field}="attributes.{field}" | __error__="" | unwrap {field})'
            f"[{duration_s}s]))"
        )
    return out


def _metrics_queries(
    *,
    era: LokiReadEra = LokiReadEra.LEGACY,
    duration_s: int = _DAY_S,
    indexed_labeled: bool = False,
) -> dict[str, str]:
    """The per-agent instant queries for one day's turn/exec metrics row."""
    turn = _event_pipeline(
        era=era,
        event_names=["turn_end"],
        telemetry_only=True,
        indexed_labeled=indexed_labeled,
    )
    exec_ok = _event_pipeline(
        era=era,
        event_names=["exec"],
        telemetry_only=False,
        indexed_labeled=indexed_labeled,
    )
    exec_failed = _event_pipeline(
        era=era,
        event_names=["exec_.+", "exec\\(.*"],
        telemetry_only=False,
        indexed_labeled=indexed_labeled,
    )
    dur = ' | json duration_seconds="attributes.duration_seconds" | __error__="" | unwrap duration_seconds'
    hist = (
        ' | json duration_seconds="attributes.duration_seconds"'
        ' | __error__="" | line_format "{{ floor .duration_seconds }}" | pattern "<bucket>"'
    )
    return {
        "turn_total": f"sum by (agent_id) (count_over_time(({turn})[{duration_s}s]))",
        "turn_ok": (
            f"sum by (agent_id) (count_over_time(({turn}"
            f' | json ok="attributes.ok" | ok="true")[{duration_s}s]))'
        ),
        "turn_dur_sum": f"sum by (agent_id) (sum_over_time(({turn}{dur})[{duration_s}s]))",
        "turn_dur_min": f"min by (agent_id) (min_over_time(({turn}{dur})[{duration_s}s]))",
        "turn_dur_max": f"max by (agent_id) (max_over_time(({turn}{dur})[{duration_s}s]))",
        "turn_dur_hist": (
            f"sum by (agent_id, bucket) (count_over_time(({turn}{hist})[{duration_s}s]))"
        ),
        "exec_ok": f"sum by (agent_id) (count_over_time(({exec_ok})[{duration_s}s]))",
        "exec_failed": f"sum by (agent_id) (count_over_time(({exec_failed})[{duration_s}s]))",
    }


# ── Loki I/O ─────────────────────────────────────────────────────────────────


def _query_instant(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
    """One Loki instant query; returns [(labels, value)]. Raises on transport
    or HTTP failure — the daemon pass reports and retries next round (a
    silently-zero day would be worse than a loud skip). The process-local
    capacity-one budget also governs resolution.py, which imports this seam."""
    from shared.config import settings

    base = settings.observability.telemetry_loki_url.rstrip("/")
    params = urllib.parse.urlencode({"query": logql, "time": at.timestamp()})
    req = urllib.request.Request(f"{base}/loki/api/v1/query?{params}")  # noqa: S310 — settings-derived http(s) base
    with (
        _query_budget.slot(),
        urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp,  # noqa: S310
    ):
        payload = json.loads(resp.read())
    out: list[tuple[dict[str, str], float]] = []
    for vec in payload["data"]["result"]:
        out.append((dict(vec["metric"]), float(vec["value"][1])))
    return out


def _day_source_count(day: date, *, now: datetime) -> int | None:
    """Count all event families feeding one day's rollup; None means probe failure."""
    day_start = datetime.combine(day, datetime_time.min, tzinfo=UTC)
    day_end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=UTC)
    slices = split_index_label_window(day_start, day_end, now=now)
    source_count = 0
    try:
        for slice_ in slices:
            duration_s = max(1, int((slice_.end - slice_.start).total_seconds()))
            pipeline = _event_pipeline(
                era=slice_.era,
                event_names=_ROLLUP_EVENT_NAMES,
                telemetry_only=False,
                indexed_labeled=len(slices) == 2 and slice_.era is LokiReadEra.INDEXED,
            )
            logql = f"sum(count_over_time(({pipeline})[{duration_s}s]))"
            source_count += int(sum(value for _labels, value in _query_instant(logql, slice_.end)))
    except Exception as exc:
        logger.warning(
            f"[events-maintenance] source-count probe failed for {day}: {exc}; "
            "treating the day as dirty and preserving its watermark"
        )
        return None
    return source_count


def _day_aggregates(day: date, *, now: datetime) -> tuple[list[TokensRow], list[MetricsRow]] | None:
    """Aggregate one UTC day, or return None for a zero-row indexed slice."""
    day_start = datetime.combine(day, datetime_time.min, tzinfo=UTC)
    day_end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=UTC)

    tok: dict[tuple[int, str], dict[str, float]] = {}
    met: dict[int, dict[str, float]] = {}
    hist: dict[int, dict[int, int]] = {}
    slices = split_index_label_window(day_start, day_end, now=now)
    for slice_ in slices:
        slice_row_count = 0
        duration_s = max(1, int((slice_.end - slice_.start).total_seconds()))
        indexed_labeled = len(slices) == 2 and slice_.era is LokiReadEra.INDEXED
        for name, logql in _tokens_queries(
            era=slice_.era,
            duration_s=duration_s,
            indexed_labeled=indexed_labeled,
        ).items():
            result_rows = _query_instant(logql, slice_.end)
            slice_row_count += len(result_rows)
            for labels, value in result_rows:
                key = (int(labels["agent_id"]), labels.get("model", ""))
                values = tok.setdefault(key, {})
                values[name] = values.get(name, 0.0) + value
        for name, logql in _metrics_queries(
            era=slice_.era,
            duration_s=duration_s,
            indexed_labeled=indexed_labeled,
        ).items():
            result_rows = _query_instant(logql, slice_.end)
            slice_row_count += len(result_rows)
            for labels, value in result_rows:
                agent_id = int(labels["agent_id"])
                if name == "turn_dur_hist":
                    try:
                        bucket = int(labels["bucket"])
                    except (KeyError, ValueError):
                        continue
                    met.setdefault(agent_id, {})
                    agent_hist = hist.setdefault(agent_id, {})
                    agent_hist[bucket] = agent_hist.get(bucket, 0) + int(value)
                    continue
                values = met.setdefault(agent_id, {})
                if name == "turn_dur_min":
                    values[name] = min(values.get(name, value), value)
                elif name == "turn_dur_max":
                    values[name] = max(values.get(name, value), value)
                else:
                    values[name] = values.get(name, 0.0) + value
        if slice_.era is LokiReadEra.INDEXED and slice_row_count == 0:
            return None
    tokens_rows = [
        TokensRow(
            agent_id=agent_id,
            model=model,
            calls=int(v.get("calls", 0)),
            costed_calls=int(v.get("costed_calls", 0)),
            unpriced_calls=int(v.get("calls", 0)) - int(v.get("costed_calls", 0)),
            tokens_in=int(v.get("tokens_in", 0)),
            tokens_out=int(v.get("tokens_out", 0)),
            tokens_cached=int(v.get("tokens_cached", 0)),
            tokens_reasoning=int(v.get("tokens_reasoning", 0)),
            cost_usd=float(v.get("cost_usd", 0.0)),
        )
        for (agent_id, model), v in tok.items()
    ]

    metrics_rows = [
        MetricsRow(
            agent_id=agent_id,
            turn_total=int(v.get("turn_total", 0)),
            turn_ok=int(v.get("turn_ok", 0)),
            turn_dur_sum=float(v.get("turn_dur_sum", 0.0)),
            turn_dur_min=v.get("turn_dur_min"),
            turn_dur_max=v.get("turn_dur_max"),
            turn_dur_hist=hist.get(agent_id, {}),
            exec_ok=int(v.get("exec_ok", 0)),
            exec_failed=int(v.get("exec_failed", 0)),
        )
        for agent_id, v in met.items()
    ]
    return tokens_rows, metrics_rows


# ── upserts ──────────────────────────────────────────────────────────────────

_TOKENS_UPSERT = """
    INSERT INTO agent_model_tokens_daily
        (agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached,
         tokens_reasoning, cost_usd, costed_calls, unpriced_calls)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (agent_id, day, model) DO UPDATE SET
        llm_calls        = EXCLUDED.llm_calls,
        tokens_in        = EXCLUDED.tokens_in,
        tokens_out       = EXCLUDED.tokens_out,
        tokens_cached    = EXCLUDED.tokens_cached,
        tokens_reasoning = EXCLUDED.tokens_reasoning,
        cost_usd         = EXCLUDED.cost_usd,
        costed_calls     = EXCLUDED.costed_calls,
        unpriced_calls   = EXCLUDED.unpriced_calls
"""

_METRICS_UPSERT = """
    INSERT INTO agent_metrics_daily
        (agent_id, day, turn_total, turn_ok, turn_dur_sum, turn_dur_min,
         turn_dur_max, turn_dur_hist, exec_ok, exec_failed)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    ON CONFLICT (agent_id, day) DO UPDATE SET
        turn_total   = EXCLUDED.turn_total,
        turn_ok      = EXCLUDED.turn_ok,
        turn_dur_sum = EXCLUDED.turn_dur_sum,
        turn_dur_min = EXCLUDED.turn_dur_min,
        turn_dur_max = EXCLUDED.turn_dur_max,
        turn_dur_hist = EXCLUDED.turn_dur_hist,
        exec_ok      = EXCLUDED.exec_ok,
        exec_failed  = EXCLUDED.exec_failed
"""

_STATE_UPSERT = """
    INSERT INTO rollup_day_state (day, status, source_count, rolled_at, error)
    VALUES (%s, %s, %s, now(), %s)
    ON CONFLICT (day) DO UPDATE SET
        status       = EXCLUDED.status,
        source_count = EXCLUDED.source_count,
        rolled_at    = EXCLUDED.rolled_at,
        error        = EXCLUDED.error
"""


def _max_rolled_day(cur: psycopg.Cursor, table: str) -> date | None:
    """The newest `day` already rolled into `table` (None = empty). `table` is
    a fixed internal literal; composed via sql.Identifier for safety."""
    cur.execute(sql.SQL("SELECT max(day) FROM {}").format(sql.Identifier(table)))
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate without GROUP BY always returns one row
    return row[0]


def _rollup_day_states(
    cur: psycopg.Cursor, *, floor_day: date, yesterday: date
) -> dict[date, _RollupDayState]:
    cur.execute(
        "SELECT day, status, source_count FROM rollup_day_state WHERE day BETWEEN %s AND %s",
        (floor_day, yesterday),
    )
    return {
        row[0]: _RollupDayState(status=str(row[1]), source_count=int(row[2]))
        for row in cur.fetchall()
    }


def _candidate_days(
    states: dict[date, _RollupDayState],
    *,
    floor_day: date,
    yesterday: date,
    lookback_days: int,
) -> list[date]:
    """Recent probe range plus any older retained failure that still needs retry."""
    if floor_day > yesterday:
        return []
    start_day = max(max(states) - timedelta(days=lookback_days), floor_day) if states else floor_day
    days: set[date] = set()
    day = start_day
    while day <= yesterday:
        days.add(day)
        day += timedelta(days=1)
    days.update(day for day, state in states.items() if state.status == "failed")
    return sorted(days)


def _prepare_dirty_days(
    states: dict[date, _RollupDayState],
    candidates: list[date],
    *,
    now: datetime,
    yesterday: date,
    lookback_days: int,
    pass_started: float,
    pass_deadline_s: float,
) -> tuple[list[_DayRollup], list[_StateUpdate], list[date]]:
    """Run bounded Loki probes/recomputes without holding a DB transaction."""

    def deadline_reached() -> bool:
        return time.monotonic() - pass_started >= pass_deadline_s

    def warn_deadline(remaining: list[date]) -> None:
        logger.warning(
            "[events-maintenance] rollup pass deadline reached; remaining "
            f"candidate/dirty days: {[str(day) for day in remaining]}"
        )

    late_write_floor = yesterday - timedelta(days=lookback_days) + timedelta(days=1)
    per_day: list[_DayRollup] = []
    state_updates: list[_StateUpdate] = []
    attempted_days: list[date] = []
    for index, day in enumerate(candidates):
        if deadline_reached():
            warn_deadline(candidates[index:])
            break
        probe_count = _day_source_count(day, now=now)
        state = states.get(day)
        dirty = (
            state is None
            or state.status == "failed"
            or probe_count is None
            or state.source_count != probe_count
            or day >= late_write_floor
        )
        if not dirty:
            continue
        if probe_count is None:
            logger.warning(
                f"[events-maintenance] source-count probe failed for {day}; "
                "rerolling while preserving the previous watermark"
            )
        if deadline_reached():
            warn_deadline(candidates[index:])
            break
        attempted_days.append(day)
        aggregates = _day_aggregates(day, now=now)
        if aggregates is None:
            error = "indexed slice returned zero rows"
            logger.warning(
                f"[events-maintenance] {error} for {day}; refusing to rewrite "
                "that day and leaving existing rollup rows intact"
            )
            state_updates.append(
                (day, "failed", state.source_count if state is not None else 0, error)
            )
            continue
        tokens_rows, metrics_rows = aggregates
        per_day.append((day, tokens_rows, metrics_rows))
        if probe_count is not None:
            state_updates.append((day, "rolled", probe_count, None))
    return per_day, state_updates, attempted_days


def _write_rollup(
    conn: psycopg.Connection,
    per_day: list[_DayRollup],
    state_updates: list[_StateUpdate],
) -> tuple[int, int]:
    """Atomically write prepared ledger rows and their successful/failure states."""
    tokens_count = metrics_count = 0
    skipped_tokens_count = skipped_metrics_count = 0
    skipped_agent_ids: set[int] = set()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT id FROM agents")
        known_agent_ids = {int(row[0]) for row in cur.fetchall()}
        for day, tokens_rows, metrics_rows in per_day:
            for token_row in tokens_rows:
                if token_row.agent_id not in known_agent_ids:
                    skipped_agent_ids.add(token_row.agent_id)
                    skipped_tokens_count += 1
                    continue
                cur.execute(
                    _TOKENS_UPSERT,
                    (
                        token_row.agent_id,
                        day,
                        token_row.model,
                        token_row.calls,
                        token_row.tokens_in,
                        token_row.tokens_out,
                        token_row.tokens_cached,
                        token_row.tokens_reasoning,
                        token_row.cost_usd,
                        token_row.costed_calls,
                        token_row.unpriced_calls,
                    ),
                )
                tokens_count += cur.rowcount
            for metrics_row in metrics_rows:
                if metrics_row.agent_id not in known_agent_ids:
                    skipped_agent_ids.add(metrics_row.agent_id)
                    skipped_metrics_count += 1
                    continue
                cur.execute(
                    _METRICS_UPSERT,
                    (
                        metrics_row.agent_id,
                        day,
                        metrics_row.turn_total,
                        metrics_row.turn_ok,
                        metrics_row.turn_dur_sum,
                        metrics_row.turn_dur_min,
                        metrics_row.turn_dur_max,
                        json.dumps(metrics_row.turn_dur_hist),
                        metrics_row.exec_ok,
                        metrics_row.exec_failed,
                    ),
                )
                metrics_count += cur.rowcount
        for state_update in state_updates:
            cur.execute(_STATE_UPSERT, state_update)
    if skipped_agent_ids:
        logger.warning(
            f"[events-maintenance] skipped rollup rows for unknown agent ids "
            f"{sorted(skipped_agent_ids)}; tokens rows dropped: {skipped_tokens_count}, "
            f"metrics rows dropped: {skipped_metrics_count}"
        )
    return metrics_count, tokens_count


def compute_rollup(
    conn: psycopg.Connection,
    *,
    now_utc: datetime,
    lookback_days: int | None = None,
    pass_deadline_s: float | None = None,
) -> RollupResult:
    """Probe retained closed days and fully reroll only dirty watermarks.

    Loki I/O finishes before the write transaction opens. Ledger and watermark
    upserts then commit atomically, so a state row never advertises a roll that
    did not land. A retention gap is still unrecoverable without the JSONL
    mirror and is reported before the retained candidate scan begins.
    """
    from shared.config import settings

    pass_started = time.monotonic()
    if lookback_days is None:
        lookback_days = settings.daemon.events_rollup_late_write_lookback_days
    if pass_deadline_s is None:
        pass_deadline_s = settings.daemon.events_rollup_pass_deadline_s
    now = now_utc.astimezone(UTC)
    yesterday = now.date() - timedelta(days=1)
    floor_day = (now - EVENT_STREAM_RETENTION).date() + timedelta(days=1)

    with conn.cursor() as cur:
        max_metrics = _max_rolled_day(cur, "agent_metrics_daily")
        max_tokens = _max_rolled_day(cur, "agent_model_tokens_daily")
        states = _rollup_day_states(cur, floor_day=floor_day, yesterday=yesterday)
    processed = [day for day in (max_metrics, max_tokens) if day is not None]
    if processed and max(processed) < floor_day - timedelta(days=1):
        logger.warning(
            "[events-maintenance] rollup gap exceeds Loki retention: "
            f"last rolled day {max(processed)}, retention floor {floor_day} — "
            "the days between are not aggregatable and stay missing "
            "(manual recovery source: the JSONL telemetry mirror)"
        )

    candidates = _candidate_days(
        states,
        floor_day=floor_day,
        yesterday=yesterday,
        lookback_days=lookback_days,
    )
    if not candidates:
        return RollupResult(None, None, 0, 0)

    per_day, state_updates, attempted_days = _prepare_dirty_days(
        states,
        candidates,
        now=now,
        yesterday=yesterday,
        lookback_days=lookback_days,
        pass_started=pass_started,
        pass_deadline_s=pass_deadline_s,
    )
    if not attempted_days:
        return RollupResult(None, None, 0, 0)
    metrics_count, tokens_count = _write_rollup(conn, per_day, state_updates)
    return RollupResult(min(attempted_days), max(attempted_days), metrics_count, tokens_count)
