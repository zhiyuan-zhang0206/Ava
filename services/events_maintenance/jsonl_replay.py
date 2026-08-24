"""Replay pre-Loki-retention ledger gaps from the filtered JSONL mirror."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
from psycopg import sql

import shared.db
from services.events_maintenance.rollup import (
    _METRICS_UPSERT,
    _TOKENS_UPSERT,
    MetricsRow,
    TokensRow,
)
from shared.log import logger
from shared.loki_index_labels import EVENT_STREAM_RETENTION
from shared.paths import logs_dir
from shared.telemetry import _is_rollup_source


@dataclass(frozen=True)
class ReplayResult:
    """Ledger days and row counts produced by one replay pass."""

    days_replayed: list[date]
    days_failed: list[date]
    tokens_rows: int
    metrics_rows: int


def aggregate_rollup_file(path: Path) -> tuple[list[TokensRow], list[MetricsRow], set[int]]:
    """Aggregate one rollup-source mirror file.

    The returned ID set is every usable source agent ID; the replay caller
    reconciles it against the live ``agents`` table before writing.
    """
    tokens: dict[tuple[int, str], dict[str, float]] = {}
    metrics: dict[int, dict[str, float]] = {}
    source_agent_ids: set[int] = set()

    with path.open(encoding="utf-8") as source:
        for line in source:
            event = json.loads(line)
            event_name = str(event["event_name"])
            if not _is_rollup_source(event_name):
                continue
            try:
                agent_id = int(event["agent_id"])
            except (KeyError, TypeError, ValueError):
                continue
            source_agent_ids.add(agent_id)
            attributes = event["attributes"]

            if event_name == "llm_usage":
                key = (agent_id, str(attributes.get("model", "")))
                values = tokens.setdefault(key, {})
                values["calls"] = values.get("calls", 0.0) + 1
                cost = attributes.get("cost_usd", "")
                if "cost_usd" in attributes and cost != "":
                    values["costed_calls"] = values.get("costed_calls", 0.0) + 1
                for name, field in (
                    ("tokens_in", "in_total"),
                    ("tokens_out", "out_total"),
                    ("tokens_cached", "cache_read"),
                    ("tokens_reasoning", "reasoning"),
                ):
                    values[name] = values.get(name, 0.0) + float(attributes.get(field, 0))
                values["cost_usd"] = values.get("cost_usd", 0.0) + float(cost or 0.0)
                continue

            values = metrics.setdefault(agent_id, {})
            if event_name == "turn_end":
                values["turn_total"] = values.get("turn_total", 0.0) + 1
                if attributes.get("ok") is True:
                    values["turn_ok"] = values.get("turn_ok", 0.0) + 1
                if "duration_seconds" in attributes:
                    duration = float(attributes["duration_seconds"])
                    values["turn_dur_sum"] = values.get("turn_dur_sum", 0.0) + duration
                    values["turn_dur_min"] = min(values.get("turn_dur_min", duration), duration)
                    values["turn_dur_max"] = max(values.get("turn_dur_max", duration), duration)
            elif event_name == "exec":
                values["exec_ok"] = values.get("exec_ok", 0.0) + 1
            else:
                values["exec_failed"] = values.get("exec_failed", 0.0) + 1

    tokens_rows = [
        TokensRow(
            agent_id=agent_id,
            model=model,
            calls=int(values.get("calls", 0)),
            costed_calls=int(values.get("costed_calls", 0)),
            unpriced_calls=int(values.get("calls", 0)) - int(values.get("costed_calls", 0)),
            tokens_in=int(values.get("tokens_in", 0)),
            tokens_out=int(values.get("tokens_out", 0)),
            tokens_cached=int(values.get("tokens_cached", 0)),
            tokens_reasoning=int(values.get("tokens_reasoning", 0)),
            cost_usd=float(values.get("cost_usd", 0.0)),
        )
        for (agent_id, model), values in sorted(tokens.items())
    ]
    metrics_rows = [
        MetricsRow(
            agent_id=agent_id,
            turn_total=int(values.get("turn_total", 0)),
            turn_ok=int(values.get("turn_ok", 0)),
            turn_dur_sum=float(values.get("turn_dur_sum", 0.0)),
            turn_dur_min=values.get("turn_dur_min"),
            turn_dur_max=values.get("turn_dur_max"),
            exec_ok=int(values.get("exec_ok", 0)),
            exec_failed=int(values.get("exec_failed", 0)),
        )
        for agent_id, values in sorted(metrics.items())
    ]
    return tokens_rows, metrics_rows, source_agent_ids


def replay_gap_days(
    conn: psycopg.Connection, *, now_utc: datetime, dry_run: bool = False
) -> ReplayResult:
    """Replay ledger gaps older than Loki's fully retained day range."""
    return _replay_gap_days(conn, now_utc=now_utc, dry_run=dry_run, selected_days=None)


def _max_day_before(cur: psycopg.Cursor, table: str, floor_day: date) -> date | None:
    """Newest ledger day below the Loki floor for one fixed internal table."""
    cur.execute(
        sql.SQL("SELECT max(day) FROM {} WHERE day < %s").format(sql.Identifier(table)),
        (floor_day,),
    )
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate without GROUP BY always returns one row
    return row[0]


def _gap_days(
    conn: psycopg.Connection, *, now_utc: datetime, selected_days: set[date] | None
) -> list[date]:
    """Contiguous ledger gap ending immediately before Loki's safe floor.

    The daemon invokes replay after the Loki rollup. Restricting the watermark
    query below the floor prevents those freshly written retained days from
    hiding the older discontinuity that replay exists to repair.
    """
    now = now_utc.astimezone(UTC)
    floor_day = (now - EVENT_STREAM_RETENTION).date() + timedelta(days=1)
    with conn.cursor() as cur:
        max_metrics = _max_day_before(cur, "agent_metrics_daily", floor_day)
        max_tokens = _max_day_before(cur, "agent_model_tokens_daily", floor_day)
    processed = [day for day in (max_metrics, max_tokens) if day is not None]
    if not processed:
        return []
    start_day = max(processed) + timedelta(days=1)
    end_day = floor_day - timedelta(days=1)
    days: list[date] = []
    day = start_day
    while day <= end_day:
        if selected_days is None or day in selected_days:
            days.append(day)
        day += timedelta(days=1)
    return days


def _known_agent_ids(conn: psycopg.Connection) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agents")
        return {int(row[0]) for row in cur.fetchall()}


def _upsert_day(
    conn: psycopg.Connection,
    day: date,
    tokens_rows: list[TokensRow],
    metrics_rows: list[MetricsRow],
) -> tuple[int, int]:
    tokens_count = metrics_count = 0
    with conn.transaction(), conn.cursor() as cur:
        for row in tokens_rows:
            cur.execute(
                _TOKENS_UPSERT,
                (
                    row.agent_id,
                    day,
                    row.model,
                    row.calls,
                    row.tokens_in,
                    row.tokens_out,
                    row.tokens_cached,
                    row.tokens_reasoning,
                    row.cost_usd,
                    row.costed_calls,
                    row.unpriced_calls,
                ),
            )
            tokens_count += cur.rowcount
        for row in metrics_rows:
            cur.execute(
                _METRICS_UPSERT,
                (
                    row.agent_id,
                    day,
                    row.turn_total,
                    row.turn_ok,
                    row.turn_dur_sum,
                    row.turn_dur_min,
                    row.turn_dur_max,
                    row.exec_ok,
                    row.exec_failed,
                ),
            )
            metrics_count += cur.rowcount
    return tokens_count, metrics_count


def _replay_gap_days(
    conn: psycopg.Connection,
    *,
    now_utc: datetime,
    dry_run: bool,
    selected_days: set[date] | None,
) -> ReplayResult:
    gap_days = _gap_days(conn, now_utc=now_utc, selected_days=selected_days)
    known_agent_ids = _known_agent_ids(conn)
    days_replayed: list[date] = []
    days_failed: list[date] = []
    tokens_count = metrics_count = 0

    for day in gap_days:
        path = logs_dir() / f"events-{day:%Y%m%d}.rollup.jsonl"
        if not path.is_file():
            continue
        tokens_rows, metrics_rows, source_agent_ids = aggregate_rollup_file(path)
        known_tokens = [row for row in tokens_rows if row.agent_id in known_agent_ids]
        known_metrics = [row for row in metrics_rows if row.agent_id in known_agent_ids]
        unknown_agent_ids = source_agent_ids - known_agent_ids
        if unknown_agent_ids:
            logger.warning(
                f"[events-maintenance] JSONL replay {day} skipped unknown agent ids "
                f"{sorted(unknown_agent_ids)}; tokens rows dropped: "
                f"{len(tokens_rows) - len(known_tokens)}, metrics rows dropped: "
                f"{len(metrics_rows) - len(known_metrics)}"
            )
        if not tokens_rows and not metrics_rows:
            logger.error(
                f"[events-maintenance] JSONL replay source for {day} aggregated zero rows; "
                "refusing to count the day as replayed"
            )
            days_failed.append(day)
            break
        if not known_tokens and not known_metrics:
            logger.error(
                f"[events-maintenance] JSONL replay source for {day} contained only "
                "unknown-agent rows; refusing to count the day as replayed"
            )
            days_failed.append(day)
            break
        if dry_run:
            day_tokens = len(known_tokens)
            day_metrics = len(known_metrics)
            logger.info(
                f"[events-maintenance] JSONL replay dry run would write {day}: "
                f"{day_tokens} token rows, {day_metrics} metric rows"
            )
        else:
            day_tokens, day_metrics = _upsert_day(conn, day, known_tokens, known_metrics)
        tokens_count += day_tokens
        metrics_count += day_metrics
        days_replayed.append(day)

    return ReplayResult(days_replayed, days_failed, tokens_count, metrics_count)


def _day_arg(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid UTC day {value!r}; expected YYYYMMDD") from exc


def _days_summary(days: list[date]) -> str:
    return ",".join(str(day) for day in days) or "-"


def main(argv: list[str] | None = None) -> int:
    """CLI entry for explicit dry-runs or selected historical gap days."""
    parser = argparse.ArgumentParser(
        prog="python -m services.events_maintenance.jsonl_replay",
        description="Replay pre-Loki-retention ledger gaps from rollup JSONL mirrors.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report rows without writing them.")
    parser.add_argument(
        "--days",
        nargs="+",
        type=_day_arg,
        help="Replay only these UTC days (YYYYMMDD), if they are still in the ledger gap.",
    )
    args = parser.parse_args(argv)
    selected_days = set(cast("list[date]", args.days)) if args.days is not None else None
    with shared.db.connect() as conn:
        result = _replay_gap_days(
            conn,
            now_utc=datetime.now(UTC),
            dry_run=bool(args.dry_run),
            selected_days=selected_days,
        )
    sys.stdout.write(
        f"replayed={_days_summary(result.days_replayed)} "
        f"failed={_days_summary(result.days_failed)} "
        f"tokens_rows={result.tokens_rows} metrics_rows={result.metrics_rows}\n"
    )
    return 1 if result.days_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
