#!/usr/bin/env python3
"""Rebuild `llm_usage_hourly` from the frozen LLM-usage event extract.

The hourly LLM cost/usage curve for rows before 2026-08-13 survives only in the
frozen 2026-08-28 cold PG events archive: Loki's 7d retention already dropped
that window, so the archive's JSONL extract is the single remaining source. This
script folds that extract into (UTC hour x model) totals and upserts them.

Operator-run at a coordinated window — the migration that creates the table does
NOT call it, so the write lands when an operator decides, not on the next
`ava start`. It is re-runnable: every column is derived from the extract, so a
second run over the same file rewrites identical values.

    .venv/bin/python scripts/backfill_llm_usage_hourly.py \
        --input ~/.ava/workspaces/5804/llm-usage-extract-full.jsonl --dry-run
    .venv/bin/python scripts/backfill_llm_usage_hourly.py \
        --input ~/.ava/workspaces/5804/llm-usage-extract-full.jsonl

Row shape is the `llm_usage` event payload (`shared/events/contract.py:LlmUsage`):
`ts` plus an `attributes` object carrying the four token counters and, when the
call was priced, the usage-time `cost_usd` snapshot.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.config import settings
from shared.db_transaction import write_transaction

# The 2x peak-price window opened at this instant. It starts AFTER the extract's
# last row (2026-08-13), so on today's archive every hour prices as off-peak and
# the two cost columns agree — the split is carried anyway so a later extract
# that does cross the boundary needs no code change.
PEAK_WINDOW_START = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
PEAK_RATIO = 2.0

# A pre-pricing row (or a call on a model with no known price) carries no model
# and no cost. Per the LlmUsage contract an absent cost means unpriced, never
# unknown, so such a row contributes real tokens at zero cost under one bucket.
UNKNOWN_MODEL = "unknown"

_PREVIEW_ROWS = 5

_UPSERT = """
INSERT INTO llm_usage_hourly (
    ts_hour, model, in_total, cache_read, out_total, reasoning,
    cost_peak_usd, cost_offpeak_usd
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ts_hour, model) DO UPDATE SET
    in_total         = EXCLUDED.in_total,
    cache_read       = EXCLUDED.cache_read,
    out_total        = EXCLUDED.out_total,
    reasoning        = EXCLUDED.reasoning,
    cost_peak_usd    = EXCLUDED.cost_peak_usd,
    cost_offpeak_usd = EXCLUDED.cost_offpeak_usd
"""


@dataclass(frozen=True)
class HourlyTotals:
    """One `llm_usage_hourly` row's payload — the totals for (ts_hour, model)."""

    in_total: int
    cache_read: int
    out_total: int
    reasoning: int
    cost_peak_usd: float
    cost_offpeak_usd: float


@dataclass
class _Running:
    """Mutable accumulator; the peak/off-peak split is applied once at the end."""

    in_total: int = 0
    cache_read: int = 0
    out_total: int = 0
    reasoning: int = 0
    cost_usd: float = 0.0


def _hour_bucket(raw_ts: str) -> datetime:
    """The UTC hour a row's timestamp falls in.

    A naive timestamp is refused rather than assumed local: `astimezone` would
    silently bucket it against this host's offset, which is exactly the kind of
    off-by-one-hour curve that cannot be spotted after the fact.
    """
    ts = datetime.fromisoformat(raw_ts)
    if ts.tzinfo is None:
        raise ValueError(f"llm_usage row timestamp carries no timezone: {raw_ts!r}")
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def aggregate_llm_usage(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[datetime, str], HourlyTotals]:
    """Fold `llm_usage` rows into (UTC hour, model) totals. Pure — no DB, no clock."""
    running: defaultdict[tuple[datetime, str], _Running] = defaultdict(_Running)
    for row in rows:
        attributes = row["attributes"]
        key = (_hour_bucket(row["ts"]), attributes.get("model") or UNKNOWN_MODEL)
        acc = running[key]
        acc.in_total += attributes["in_total"]
        acc.cache_read += attributes["cache_read"]
        acc.out_total += attributes["out_total"]
        acc.reasoning += attributes["reasoning"]
        acc.cost_usd += attributes.get("cost_usd", 0.0)
    return {
        (ts_hour, model): HourlyTotals(
            in_total=acc.in_total,
            cache_read=acc.cache_read,
            out_total=acc.out_total,
            reasoning=acc.reasoning,
            cost_peak_usd=acc.cost_usd * (PEAK_RATIO if ts_hour >= PEAK_WINDOW_START else 1.0),
            cost_offpeak_usd=acc.cost_usd,
        )
        for (ts_hour, model), acc in running.items()
    }


def read_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream the extract's JSONL rows — 135k lines, so never materialized whole."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


@contextmanager
def _dialing(db_url: str) -> Generator[None]:
    """Point `shared.db` at `db_url` for the duration.

    The connection helpers read `settings.data_plane.db_url` rather than take a
    URL, and going through them is what keeps the guard, the keepalives, and the
    pooled-session scrub — so `--db-url` is expressed as an override of that one
    field instead of a hand-rolled `psycopg.connect`.
    """
    original = settings.data_plane.db_url
    settings.data_plane.db_url = db_url
    try:
        yield
    finally:
        settings.data_plane.db_url = original


def upsert_totals(totals: Mapping[tuple[datetime, str], HourlyTotals], *, db_url: str) -> int:
    """Upsert every aggregated row in one explicitly writable transaction."""
    params = [
        (
            ts_hour,
            model,
            row.in_total,
            row.cache_read,
            row.out_total,
            row.reasoning,
            row.cost_peak_usd,
            row.cost_offpeak_usd,
        )
        for (ts_hour, model), row in sorted(totals.items())
    ]
    with _dialing(db_url), write_transaction() as conn, conn.cursor() as cur:
        cur.executemany(_UPSERT, params)
    return len(params)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="the llm_usage JSONL extract from the frozen events archive",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="target cluster database (default: this checkout's AVA_DB_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written and exit without touching the database",
    )
    args = parser.parse_args(argv)

    totals = aggregate_llm_usage(read_rows(args.input))
    print(f"{len(totals)} (hour, model) group(s) aggregated from {args.input}")
    for key in sorted(totals)[:_PREVIEW_ROWS]:
        ts_hour, model = key
        row = totals[key]
        print(
            f"  {ts_hour.isoformat()} {model} in={row.in_total} cache={row.cache_read} "
            f"out={row.out_total} reasoning={row.reasoning} "
            f"peak=${row.cost_peak_usd:.4f} offpeak=${row.cost_offpeak_usd:.4f}"
        )
    if args.dry_run:
        print("dry-run — nothing written")
        return 0

    written = upsert_totals(totals, db_url=args.db_url or settings.data_plane.db_url)
    print(f"upserted {written} row(s) into llm_usage_hourly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
