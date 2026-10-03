#!/usr/bin/env python3
"""Backfill `telemetry_events` from the stores that held telemetry and log events before it.

Before `telemetry_events` the telemetry and log events lived only in the event stream: the
Loki live stream (84 hours), the local JSONL mirrors (7 days of every event, per machine) and,
for the era before 2026-08-13, the Loki archive stream. This script folds them into the table so
the readers that moved to Postgres see the history. The local mirror of the machine that runs the
events-maintenance daemon is also replayed by that daemon every hour; this script covers the
other machines' mirrors, the Loki windows, and the archive.

Operator-run at the window where the table starts being written; it is not part of any
migration. Safe to run while the live write path is on and to re-run:

- every row is keyed by `(event_uid, ts)` (the stream's surrogate id, the same value Loki and the
  JSONL carry for an event) and inserted `ON CONFLICT DO NOTHING`, so a row the live path already
  wrote, and a row two sources both hold, is kept once and never overwritten;
- `imported_from` names the source of every row this script inserts (`jsonl`, `loki-live`,
  `loki-archive`); live rows keep NULL;
- the writes commit in batches, so an interrupted run leaves a prefix that the next run skips.

Dry-run is the default: it reads every source and prints the reconciliation (rows read, skipped
and why, duplicates across sources, rows already in the table, rows that would be inserted,
counts by category and event name) without writing. `--apply` re-reads and inserts.

    .venv/bin/python scripts/data_repair/backfill_telemetry_events.py --jsonl ~/.ava/logs
    .venv/bin/python scripts/data_repair/backfill_telemetry_events.py --jsonl DIR --loki-live --apply

Sources, in precedence order (when two hold an event the first one's row is kept):

- `--jsonl DIR` (repeatable): `events-YYYYMMDD.jsonl` of a machine. Full rows, including `cluster`.
- `--loki-live`: the live stream from `--loki-since` (default: 84 hours ago) to `--until`. Loki's
  reader does not return the `cluster` field, so these rows carry this cluster's label. A window
  Loki refuses as too large (HTTP 413) is re-read in halves; `--loki-page` bounds the page size.
- `--archive`: the Loki archive stream (2026-05-23 to 2026-08-13, about 5.9 million rows and 2 GB
  at the time of the freeze). Off by default: it restores history the 2026-08-29 cleanup dropped,
  and the operator decides whether the table should hold it.

Run it with the cluster environment of the gateway host (Loki URL and database URL come from the
settings). Rows older than the oldest source (telemetry between 2026-08-13 and the last 84 hours
that no mirror kept) are gone.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from base.db import connect
from base.db.transaction import write_transaction
from base.telemetry.event_store import ensure_partitions, insert_rows, stored_row
from base.telemetry.loki_index_labels import ARCHIVE_FLOOR_AT, ARCHIVE_FREEZE_AT
from base.telemetry.observability import cluster_label

_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_CATEGORIES = ("telemetry", "log")
_LOKI_WINDOW = timedelta(days=1)
# Loki's per-query line cap is 50001 (limits_config.max_entries_limit_per_query) and the client
# asks with one extra line for has_more, so 50000 is the largest page it can request. The default
# sits lower: at the stock page the dense telemetry stream returned a 114.7 MB response that Loki
# refused with HTTP 413 (2026-10-03 backfill), so 20000 keeps a page under the response ceiling.
_LOKI_PAGE = 20_000
_MAX_LOKI_PAGE = 50_000
# Transient transport failures retry with this backoff; a long pass should not restart over one
# blip. A refusal (HTTP 413) is not transient — its window is split instead.
_TRANSIENT_RETRY_SLEEPS_S = (1.0, 4.0)
# Loki answers a response past its message ceiling with HTTP 413 (the 114.7 MB response above);
# half the window is half the response, so a refusal is split like a full page.
_RESPONSE_TOO_LARGE = 413
_INSERT_BATCH = 2_000
_LIVE_HOURS = 84

SOURCE_ORDER = ("jsonl", "loki-live", "loki-archive")


class SkippedRowError(Exception):
    """A source row that cannot become a `telemetry_events` row; the message is the reason."""


@dataclass(frozen=True)
class Skipped:
    """A stream marker for a row that was read but not kept, with the reason."""

    reason: str


def _timestamp(raw: Any) -> datetime:
    ts = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    return ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _page_size(value: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if not 1 <= page <= _MAX_LOKI_PAGE:
        raise argparse.ArgumentTypeError(f"page size must be within 1..{_MAX_LOKI_PAGE}")
    return page


def normalize(raw: dict[str, Any], *, source: str, cluster: str) -> dict[str, Any]:
    """Map one stream row (Loki reader dict or JSONL mirror line) to an insert record.

    Raises `SkippedRowError` for a row the table's constraints would reject.
    """
    if raw.get("category") not in _CATEGORIES:
        raise SkippedRowError("not-telemetry-or-log")
    event_name = raw.get("event_name")
    if not isinstance(event_name, str) or not event_name:
        raise SkippedRowError("no-event-name")
    if str(raw.get("level") or "").lower() not in _LEVELS:
        raise SkippedRowError("bad-level")
    if not isinstance(raw.get("id"), int):
        raise SkippedRowError("no-id")
    try:
        ts = _timestamp(raw["ts"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SkippedRowError("bad-ts") from exc
    attributes = raw.get("attributes")
    return stored_row(
        {
            **raw,
            "ts": ts.isoformat(),
            "level": str(raw["level"]).lower(),
            "machine": str(raw.get("machine") or ""),
            "cluster": str(raw.get("cluster") or cluster),
            "process": str(raw.get("process") or ""),
            "source": str(raw.get("source") or ""),
            "attributes": attributes if isinstance(attributes, dict) else {},
        },
        imported_from=source,
    )


LokiQuery = Callable[[datetime, datetime, int], tuple[list[dict[str, Any]], bool]]


def _loki_windows(
    start: datetime, end: datetime, query: LokiQuery, *, page: int = _LOKI_PAGE
) -> Iterator[dict[str, Any]]:
    """Every row of [start, end] from `query(from, to, limit)`, splitting any full page.

    A window whose page comes back full is bisected until each part is complete, so no row is
    dropped by the page cap; a window Loki refuses as too large is split the same way, since half
    the window is half the response.
    """
    stack: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        stack.append((cursor, min(cursor + _LOKI_WINDOW, end)))
        cursor += _LOKI_WINDOW
    stack.reverse()
    while stack:
        lo, hi = stack.pop()
        try:
            rows, has_more = _read_page(query, lo, hi, page=page)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == _RESPONSE_TOO_LARGE:
                middle = lo + (hi - lo) / 2
                if lo < middle < hi:
                    stack.extend([(middle, hi), (lo, middle)])
                    continue
            raise
        middle = lo + (hi - lo) / 2
        if has_more and lo < middle < hi:
            stack.extend([(middle, hi), (lo, middle)])
            continue
        if has_more:
            raise RuntimeError(f"Loki window {lo.isoformat()}..{hi.isoformat()} cannot be split")
        yield from rows


def _read_page(
    query: LokiQuery, lo: datetime, hi: datetime, *, page: int
) -> tuple[list[dict[str, Any]], bool]:
    """One page read; transient transport failures retry with a bounded backoff.

    The pass reads months of history in one run; failing it over a single suspended connection
    would re-read every source on the re-run, so a few retries are cheaper. A 413 is not
    transient — it says this window is too large to read at this page, and the caller splits it.
    """
    for sleep_s in (*_TRANSIENT_RETRY_SLEEPS_S, None):
        try:
            return query(lo, hi, page)
        except httpx.HTTPStatusError as exc:
            if sleep_s is None or exc.response.status_code < 500:
                raise
        except (httpx.TimeoutException, httpx.TransportError):
            if sleep_s is None:
                raise
        time.sleep(sleep_s)
    raise AssertionError("unreachable")


def _loki_query(*, archive: bool) -> LokiQuery:
    from gateway.lgtm import loki_events

    def query(lo: datetime, hi: datetime, limit: int) -> tuple[list[dict[str, Any]], bool]:
        return loki_events.query_events(
            categories=list(_CATEGORIES),
            archive=archive,
            from_=lo,
            to=hi,
            limit=limit,
            direction="forward",
        )

    return query


def _record_or_skip(raw: dict[str, Any], source: str, cluster: str) -> dict[str, Any] | Skipped:
    try:
        return normalize(raw, source=source, cluster=cluster)
    except SkippedRowError as skipped:
        return Skipped(str(skipped))


def read_jsonl(
    directories: Iterable[Path], until: datetime, cluster: str
) -> Iterator[dict[str, Any] | Skipped]:
    """Telemetry and log rows from local event mirrors (`events-YYYYMMDD.jsonl`)."""
    for directory in directories:
        for path in sorted(directory.glob("events-????????.jsonl")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        parsed: object = json.loads(line)
                    except ValueError:
                        yield Skipped("unparsable-line")
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    raw = cast(dict[str, Any], parsed)
                    if raw.get("category") not in _CATEGORIES:
                        continue
                    record = _record_or_skip(raw, "jsonl", cluster)
                    if isinstance(record, dict) and _timestamp(record["ts"]) > until:
                        continue
                    yield record


def read_loki_live(
    since: datetime, until: datetime, cluster: str, *, page: int = _LOKI_PAGE
) -> Iterator[dict[str, Any] | Skipped]:
    """Rows of the live Loki stream between `since` and `until`."""
    for raw in _loki_windows(since, until, _loki_query(archive=False), page=page):
        yield _record_or_skip(raw, "loki-live", cluster)


def read_loki_archive(
    until: datetime, cluster: str, *, page: int = _LOKI_PAGE
) -> Iterator[dict[str, Any] | Skipped]:
    """Pre-cutover rows from the Loki archive stream."""
    end = min(ARCHIVE_FREEZE_AT, until)
    for raw in _loki_windows(ARCHIVE_FLOOR_AT, end, _loki_query(archive=True), page=page):
        yield _record_or_skip(raw, "loki-archive", cluster)


Stream = tuple[str, Iterator[dict[str, Any] | Skipped]]


def sources(args: argparse.Namespace, until: datetime) -> list[Stream]:
    """The selected sources in precedence order."""
    cluster = cluster_label()
    streams: list[Stream] = []
    if args.jsonl:
        streams.append(("jsonl", read_jsonl(args.jsonl, until, cluster)))
    if args.loki_live:
        since = args.loki_since or until - timedelta(hours=_LIVE_HOURS)
        streams.append(("loki-live", read_loki_live(since, until, cluster, page=args.loki_page)))
    if args.archive:
        streams.append(("loki-archive", read_loki_archive(until, cluster, page=args.loki_page)))
    return streams


@dataclass
class Plan:
    """What a pass over the sources found, before touching the table."""

    read: Counter[str]
    skipped: Counter[tuple[str, str]]
    duplicates: Counter[str]
    by_name: Counter[tuple[str, str]]
    first_ts: datetime | None
    last_ts: datetime | None
    keys: set[tuple[int, str]]


def plan(streams: list[Stream]) -> Plan:
    """Count what the sources hold, deduplicated by `(event_uid, ts)` in precedence order."""
    found = Plan(Counter(), Counter(), Counter(), Counter(), None, None, set())
    for name, rows in streams:
        for row in rows:
            if isinstance(row, Skipped):
                found.skipped[(name, row.reason)] += 1
                continue
            found.read[name] += 1
            key = (row["event_uid"], row["ts"])
            if key in found.keys:
                found.duplicates[name] += 1
                continue
            found.keys.add(key)
            found.by_name[(row["category"], row["event_name"])] += 1
            ts = _timestamp(row["ts"])
            found.first_ts = ts if found.first_ts is None else min(found.first_ts, ts)
            found.last_ts = ts if found.last_ts is None else max(found.last_ts, ts)
    return found


def _already_present(conn: Any, keys: set[tuple[int, str]]) -> int:
    present = 0
    ordered = sorted(keys)
    for start in range(0, len(ordered), 10_000):
        chunk = ordered[start : start + 10_000]
        row = conn.execute(
            "SELECT count(*) FROM telemetry_events t JOIN unnest(%s::bigint[], %s::timestamptz[]) "
            "AS k(uid, at) ON t.event_uid = k.uid AND t.ts = k.at",
            ([uid for uid, _ in chunk], [_timestamp(at) for _, at in chunk]),
        ).fetchone()
        present += int(row[0])
    return present


def render(found: Plan, present: int) -> str:
    """The reconciliation a dry-run prints."""
    lines = ["source          read  duplicate"]
    for name in SOURCE_ORDER:
        if found.read[name] or found.duplicates[name]:
            lines.append(f"{name:<14} {found.read[name]:>6} {found.duplicates[name]:>10}")
    for (name, reason), count in sorted(found.skipped.items()):
        lines.append(f"skipped {name}: {reason} x {count}")
    lines.append(f"distinct events: {len(found.keys)}")
    lines.append(f"already in telemetry_events: {present}")
    lines.append(f"would insert: {len(found.keys) - present}")
    if found.first_ts is not None and found.last_ts is not None:
        lines.append(f"span: {found.first_ts.isoformat()} .. {found.last_ts.isoformat()}")
    lines.append("by event name (category: count), top 25:")
    for (category, event), count in found.by_name.most_common(25):
        lines.append(f"  {event:<28} {category:<10} {count}")
    return "\n".join(lines)


def apply(streams: list[Stream], first_ts: datetime | None) -> tuple[int, int]:
    """Insert every distinct row in committed batches; returns `(inserted, attempted)`."""
    seen: set[tuple[int, str]] = set()
    inserted = attempted = 0
    batch: list[dict[str, Any]] = []
    if first_ts is not None:
        months_back = max(
            1,
            (datetime.now(UTC).year - first_ts.year) * 12
            + datetime.now(UTC).month
            - first_ts.month,
        )
        with write_transaction() as conn:
            ensure_partitions(conn, months_back=months_back)

    def flush() -> None:
        nonlocal inserted, batch
        if batch:
            with write_transaction() as conn:
                inserted += insert_rows(conn, batch)
            batch = []

    for _name, rows in streams:
        for row in rows:
            if isinstance(row, Skipped):
                continue
            key = (row["event_uid"], row["ts"])
            if key in seen:
                continue
            seen.add(key)
            attempted += 1
            batch.append(row)
            if len(batch) >= _INSERT_BATCH:
                flush()
    flush()
    return inserted, attempted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    parser.add_argument(
        "--jsonl", action="append", default=[], type=Path, metavar="DIR", help="a local logs dir"
    )
    parser.add_argument("--loki-live", action="store_true", help="read the live Loki stream")
    parser.add_argument(
        "--loki-since", type=_timestamp, default=None, help="ISO-8601 start of the live read"
    )
    parser.add_argument(
        "--loki-page",
        type=_page_size,
        default=_LOKI_PAGE,
        metavar="N",
        help=f"Loki lines per page, 1..{_MAX_LOKI_PAGE} (default: {_LOKI_PAGE})",
    )
    parser.add_argument("--archive", action="store_true", help="read the Loki archive stream")
    parser.add_argument(
        "--until",
        type=_timestamp,
        default=None,
        help="ISO-8601 upper bound (default: now); rows after it are left to the live path",
    )
    args = parser.parse_args(argv)
    if not (args.jsonl or args.loki_live or args.archive):
        parser.error("name at least one source: --jsonl DIR, --loki-live or --archive")
    until = args.until or datetime.now(UTC)
    missing = [str(path) for path in args.jsonl if not path.is_dir()]
    if missing:
        print(f"error: --jsonl directory not found: {', '.join(missing)}")
        return 1

    found = plan(sources(args, until))
    with connect() as conn:
        present = _already_present(conn, found.keys)
    print(render(found, present))
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply to write)")
        return 0
    inserted, attempted = apply(sources(args, until), found.first_ts)
    print(f"\ninserted {inserted} rows ({attempted - inserted} already present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
