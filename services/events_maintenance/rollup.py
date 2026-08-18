"""Since-Birth rollup — day-grain aggregates, sourced from Loki.

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

Same idempotence contract as the PG-sourced predecessor: a **full-day
overwrite recompute** keyed on the PK — re-running a day re-derives and
overwrites, never double-counts. Each run re-rolls the last
``lookback_days`` already-rolled days so a late OTLP write into a closed
day is picked up.

Loki bounds what is recoverable: retention is 7d
(deploy/local/lgtm/config/loki.yaml `retention_period: 168h`), so the
recompute window clamps to the first FULLY-retained day — a maintenance
outage longer than retention loses those days' aggregates (logged loudly;
the JSONL mirror remains the manual recovery source). The clamp also
protects history: a day at/below the floor is never recomputed, so
archive-backfilled rows cannot be overwritten with zeros.

Day boundary is UTC midnight; each day aggregates as Loki instant queries
evaluated at day end over a 24h range vector (Loki's (start, end] vs the
old SQL's [start, end) differ only at the exact midnight nanosecond).

Test seam: ``_day_aggregates`` is the single Loki-facing function — tests
monkeypatch it and drive the watermark/clamp/upsert logic against a real
throwaway Postgres; the LogQL builders have their own string-shape tests.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import psycopg
from psycopg import sql

from shared.log import logger

# First fully-retained day: Loki prunes past retention_period (168h), so a
# day is recomputable only when its 00:00 start is still inside retention.
_LOKI_RETENTION = timedelta(hours=168)

# The OTLP log stream selector. Single catch-all until the logs-side OTel
# Resource lands (its coordinated change rewrites every selector site).
_SELECTOR = '{service_name="unknown_service"}'

_HTTP_TIMEOUT_S = 60.0


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
    exec_ok: int
    exec_failed: int


@dataclass(frozen=True)
class RollupResult:
    """What one `compute_rollup` run covered. `start_day`/`end_day` are the
    inclusive UTC-day range recomputed (None when the run was a no-op — no
    whole retained day to roll yet); `*_rows` are the upsert row counts."""

    start_day: date | None
    end_day: date | None
    metrics_rows: int
    tokens_rows: int


# ── LogQL builders ───────────────────────────────────────────────────────────

_LLM_PIPE = f'{_SELECTOR} | agent_id!="" | category=~"telemetry|log" | event_name="llm_usage"'
_TURN_PIPE = f'{_SELECTOR} | agent_id!="" | category=~"telemetry|log" | event_name="turn_end"'
_EXEC_OK_PIPE = f'{_SELECTOR} | agent_id!="" | event_name="exec"'
# exec_failed = every exec outcome other than plain 'exec' (exec_failed /
# exec_thread_stuck / exec(timeout)) — mirrors the old SQL LIKE pair.
_EXEC_FAILED_PIPE = f'{_SELECTOR} | agent_id!="" | event_name=~"exec_.+|exec\\\\(.*"'

_DAY_S = 86400


def _tokens_queries() -> dict[str, str]:
    """The per-(agent, model) instant queries for one day's tokens/cost row.

    Structured-metadata dims (agent_id / event_name / category) filter
    directly; `model` and each numeric field need their own single-extraction
    `| json` stage (multiple extractions in one stage are a parse error)."""
    model = ' | json model="attributes.model"'
    out = {
        "calls": f"sum by (agent_id, model) (count_over_time(({_LLM_PIPE}{model})[{_DAY_S}s]))",
        "costed_calls": (
            f"sum by (agent_id, model) (count_over_time(({_LLM_PIPE}{model}"
            f' | json cost_usd="attributes.cost_usd" | cost_usd!="")[{_DAY_S}s]))'
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
            f"sum by (agent_id, model) (sum_over_time(({_LLM_PIPE}{model}"
            f' | json {field}="attributes.{field}" | __error__="" | unwrap {field})'
            f"[{_DAY_S}s]))"
        )
    return out


def _metrics_queries() -> dict[str, str]:
    """The per-agent instant queries for one day's turn/exec metrics row."""
    dur = ' | json duration_seconds="attributes.duration_seconds" | __error__="" | unwrap duration_seconds'
    return {
        "turn_total": f"sum by (agent_id) (count_over_time(({_TURN_PIPE})[{_DAY_S}s]))",
        "turn_ok": (
            f"sum by (agent_id) (count_over_time(({_TURN_PIPE}"
            f' | json ok="attributes.ok" | ok="true")[{_DAY_S}s]))'
        ),
        "turn_dur_sum": f"sum by (agent_id) (sum_over_time(({_TURN_PIPE}{dur})[{_DAY_S}s]))",
        "turn_dur_min": f"min by (agent_id) (min_over_time(({_TURN_PIPE}{dur})[{_DAY_S}s]))",
        "turn_dur_max": f"max by (agent_id) (max_over_time(({_TURN_PIPE}{dur})[{_DAY_S}s]))",
        "exec_ok": f"sum by (agent_id) (count_over_time(({_EXEC_OK_PIPE})[{_DAY_S}s]))",
        "exec_failed": f"sum by (agent_id) (count_over_time(({_EXEC_FAILED_PIPE})[{_DAY_S}s]))",
    }


# ── Loki I/O ─────────────────────────────────────────────────────────────────


def _query_instant(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
    """One Loki instant query; returns [(labels, value)]. Raises on transport
    or HTTP failure — the daemon pass reports and retries next round (a
    silently-zero day would be worse than a loud skip)."""
    from shared.config import settings

    base = settings.observability.telemetry_loki_url.rstrip("/")
    params = urllib.parse.urlencode({"query": logql, "time": at.timestamp()})
    req = urllib.request.Request(f"{base}/loki/api/v1/query?{params}")  # noqa: S310 — settings-derived http(s) base
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
        payload = json.loads(resp.read())
    out: list[tuple[dict[str, str], float]] = []
    for vec in payload["data"]["result"]:
        out.append((dict(vec["metric"]), float(vec["value"][1])))
    return out


def _day_aggregates(day: date) -> tuple[list[TokensRow], list[MetricsRow]]:
    """Aggregate one whole UTC day from Loki (the test seam — patch me)."""
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)

    tok: dict[tuple[int, str], dict[str, float]] = {}
    for name, logql in _tokens_queries().items():
        for labels, value in _query_instant(logql, day_end):
            key = (int(labels["agent_id"]), labels.get("model", ""))
            tok.setdefault(key, {})[name] = value
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

    met: dict[int, dict[str, float]] = {}
    for name, logql in _metrics_queries().items():
        for labels, value in _query_instant(logql, day_end):
            met.setdefault(int(labels["agent_id"]), {})[name] = value
    metrics_rows = [
        MetricsRow(
            agent_id=agent_id,
            turn_total=int(v.get("turn_total", 0)),
            turn_ok=int(v.get("turn_ok", 0)),
            turn_dur_sum=float(v.get("turn_dur_sum", 0.0)),
            turn_dur_min=v.get("turn_dur_min"),
            turn_dur_max=v.get("turn_dur_max"),
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
         turn_dur_max, exec_ok, exec_failed)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (agent_id, day) DO UPDATE SET
        turn_total   = EXCLUDED.turn_total,
        turn_ok      = EXCLUDED.turn_ok,
        turn_dur_sum = EXCLUDED.turn_dur_sum,
        turn_dur_min = EXCLUDED.turn_dur_min,
        turn_dur_max = EXCLUDED.turn_dur_max,
        exec_ok      = EXCLUDED.exec_ok,
        exec_failed  = EXCLUDED.exec_failed
"""


def _max_rolled_day(cur: psycopg.Cursor, table: str) -> date | None:
    """The newest `day` already rolled into `table` (None = empty). `table` is
    a fixed internal literal; composed via sql.Identifier for safety."""
    cur.execute(sql.SQL("SELECT max(day) FROM {}").format(sql.Identifier(table)))
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate without GROUP BY always returns one row
    return row[0]


def compute_rollup(
    conn: psycopg.Connection, *, now_utc: datetime, lookback_days: int
) -> RollupResult:
    """Recompute the rollup tables for the whole retained days up to yesterday.

    Range: `[start_day, yesterday]` where `start_day` =
    max(last-rolled-day - lookback, first fully-retained Loki day). An empty
    table starts at the retention floor (nothing older is aggregatable from
    Loki; older history arrives via the archive-backfill migration on
    upgraded clusters and simply does not exist on fresh ones). A gap wider
    than retention is reported loudly and skipped — those days are lost to
    the rollup unless recovered manually from the JSONL mirror.

    Watermark read + all upserts run in one transaction (atomic pass);
    re-running is idempotent (full-day overwrite). Loki I/O happens BEFORE
    the transaction opens so a slow Loki never holds locks.
    """
    now = now_utc.astimezone(UTC)
    today = now.date()
    yesterday = today - timedelta(days=1)
    floor_day = (now - _LOKI_RETENTION).date() + timedelta(days=1)

    with conn.cursor() as cur:
        max_metrics = _max_rolled_day(cur, "agent_metrics_daily")
        max_tokens = _max_rolled_day(cur, "agent_model_tokens_daily")
    # Progress marker = the furthest day either table has reached (both roll
    # over the same range each pass; a lower max means data sparsity for that
    # family, not an unprocessed gap).
    processed = [d for d in (max_metrics, max_tokens) if d is not None]
    start_day = max(processed) - timedelta(days=lookback_days) if processed else floor_day
    if start_day < floor_day:
        if processed and max(processed) < floor_day - timedelta(days=1):
            logger.warning(
                "[events-maintenance] rollup gap exceeds Loki retention: "
                f"last rolled day {max(processed)}, retention floor {floor_day} — "
                "the days between are not aggregatable and stay missing "
                "(manual recovery source: the JSONL telemetry mirror)"
            )
        start_day = floor_day
    if start_day > yesterday:
        return RollupResult(None, None, 0, 0)

    # Loki I/O outside the transaction.
    per_day: list[tuple[date, list[TokensRow], list[MetricsRow]]] = []
    day = start_day
    while day <= yesterday:
        tokens_rows, metrics_rows = _day_aggregates(day)
        per_day.append((day, tokens_rows, metrics_rows))
        day += timedelta(days=1)

    tokens_count = metrics_count = 0
    with conn.transaction(), conn.cursor() as cur:
        for day, tokens_rows, metrics_rows in per_day:
            for t in tokens_rows:
                cur.execute(
                    _TOKENS_UPSERT,
                    (
                        t.agent_id,
                        day,
                        t.model,
                        t.calls,
                        t.tokens_in,
                        t.tokens_out,
                        t.tokens_cached,
                        t.tokens_reasoning,
                        t.cost_usd,
                        t.costed_calls,
                        t.unpriced_calls,
                    ),
                )
                tokens_count += cur.rowcount
            for m in metrics_rows:
                cur.execute(
                    _METRICS_UPSERT,
                    (
                        m.agent_id,
                        day,
                        m.turn_total,
                        m.turn_ok,
                        m.turn_dur_sum,
                        m.turn_dur_min,
                        m.turn_dur_max,
                        m.exec_ok,
                        m.exec_failed,
                    ),
                )
                metrics_count += cur.rowcount

    return RollupResult(start_day, yesterday, metrics_count, tokens_count)
