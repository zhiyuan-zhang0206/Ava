#!/usr/bin/env python3
"""Backfill `audit_events` from the stores that held the audit record before it.

Before `audit_events` (decisions/2026-10-02-audit-events-in-postgres.md) the audit
facts lived only in the event stream: the Loki archive stream (every pre-cutover
row, 2026-05-23 to 2026-08-13), the Loki live stream (lineage names permanently,
every other audit name for the last 84 hours) and the local JSONL mirrors (the
last 7 days of every event, a year of the lineage names). This script folds them
into the table, so the readers that moved to Postgres see the history.

Operator-run at the window where the table starts being written; it is not part
of any migration. Safe to run while the live write path is on and to re-run:

- every row is keyed by `event_uid` (the stream's surrogate id, the same value
  Loki and the JSONL carry for an event) and inserted `ON CONFLICT DO NOTHING`,
  so a row the live path already wrote, and a row two sources both hold, is kept
  once and never overwritten;
- `imported_from` names the source of every row this script inserts
  (`loki-archive`, `loki-live`, `jsonl`); live rows keep NULL.

Dry-run is the default: it reads every source and prints the reconciliation
(rows read, rows skipped and why, duplicates across sources, rows already in the
table, rows that would be inserted, counts by event name and source) without
writing. `--apply` re-reads the sources and inserts everything in ONE
transaction, so a failure leaves the table as it was.

    .venv/bin/python scripts/data_repair/backfill_audit_events.py
    .venv/bin/python scripts/data_repair/backfill_audit_events.py --jsonl /path/to/logs --apply

Run it with the cluster environment of the gateway host (Loki URL and database
URL come from the settings). `--jsonl DIR` may be given several times: the JSONL
mirrors live on the machine that emitted the events, so a directory per machine
is how the 3.5 to 7 day window Loki already expired is recovered. A window Loki
refuses as too large (HTTP 413) is re-read in halves; `--loki-page` bounds the
page size. Rows older than the table's first live write that no source holds
(the non-lineage audit rows between 2026-08-13 and the oldest 84 hours) are gone
and are not recoverable.
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
from base.telemetry.loki_index_labels import ARCHIVE_FLOOR_AT, ARCHIVE_FREEZE_AT

_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
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
_INSERT_BATCH = 1_000
_PREVIEW = 8

_INSERT = (
    "INSERT INTO audit_events (event_uid, ts, trace_id, span_id, agent_id, machine, process, "
    "event_name, level, source, target_agent_id, attributes, imported_from) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
    "ON CONFLICT (event_uid) DO NOTHING"
)


@dataclass(frozen=True)
class AuditRow:
    """One audit event, ready to insert."""

    event_uid: int
    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    event_name: str
    level: str
    source: str
    target_agent_id: int | None
    attributes: dict[str, Any]
    imported_from: str

    def params(self) -> tuple[Any, ...]:
        return (
            self.event_uid,
            self.ts,
            self.trace_id,
            self.span_id,
            self.agent_id,
            self.machine,
            self.process,
            self.event_name,
            self.level,
            self.source,
            self.target_agent_id,
            json.dumps(self.attributes, default=str, ensure_ascii=False),
            self.imported_from,
        )


class SkippedRowError(Exception):
    """A source row that cannot become an `audit_events` row; the message is the reason."""


@dataclass(frozen=True)
class Skipped:
    """A stream marker for a row that was read but not kept, with the reason."""

    reason: str


def _signed(stream_id: int) -> int:
    """The unsigned stream id as the signed 64-bit `event_uid` (see `audit_event_uid`)."""
    return stream_id - (1 << 64) if stream_id >= 1 << 63 else stream_id


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


def normalize(raw: dict[str, Any], *, imported_from: str) -> AuditRow:
    """Map one stream row (Loki reader dict or JSONL mirror line) to an `AuditRow`.

    Raises `SkippedRowError` for a row the table's constraints would reject.
    """
    if raw.get("category") != "audit":
        raise SkippedRowError("not-audit")
    event_name = raw.get("event_name")
    if not isinstance(event_name, str) or not event_name:
        raise SkippedRowError("no-event-name")
    level = str(raw.get("level") or "").lower()
    if level not in _LEVELS:
        raise SkippedRowError("bad-level")
    stream_id = raw.get("id")
    if not isinstance(stream_id, int):
        raise SkippedRowError("no-id")
    try:
        ts = _timestamp(raw["ts"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SkippedRowError("bad-ts") from exc
    attributes = raw.get("attributes")
    return AuditRow(
        event_uid=_signed(stream_id),
        ts=ts,
        trace_id=raw.get("trace_id"),
        span_id=raw.get("span_id"),
        agent_id=raw.get("agent_id"),
        machine=str(raw.get("machine") or ""),
        process=str(raw.get("process") or ""),
        event_name=event_name,
        level=level,
        source=str(raw.get("source") or ""),
        target_agent_id=raw.get("target_agent_id"),
        attributes=cast(dict[str, Any], attributes) if isinstance(attributes, dict) else {},
        imported_from=imported_from,
    )


LokiQuery = Callable[[datetime, datetime, int], tuple[list[dict[str, Any]], bool]]


def _loki_windows(
    start: datetime, end: datetime, query: LokiQuery, *, page: int = _LOKI_PAGE
) -> Iterator[dict[str, Any]]:
    """Every row of [start, end] from `query(from, to, limit)`, splitting any full page.

    Windows of a day are read newest-agnostic (forward); a window whose page is full is bisected
    until each part is complete, so no row is dropped by the page cap. A window Loki refuses as
    too large (HTTP 413) is split the same way, since half the window is half the response.
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

    The pass reads the whole held history in one run; failing it over a single suspended
    connection would re-read every source on the re-run, so a few retries are cheaper. A 413 is
    not transient — it says this window is too large to read at this page, and the caller splits
    it.
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
            categories=["audit"], archive=archive, from_=lo, to=hi, limit=limit, direction="forward"
        )

    return query


def read_loki_archive(until: datetime, *, page: int = _LOKI_PAGE) -> Iterator[AuditRow | Skipped]:
    """Pre-cutover rows from the Loki archive stream."""
    end = min(ARCHIVE_FREEZE_AT, until)
    for raw in _loki_windows(ARCHIVE_FLOOR_AT, end, _loki_query(archive=True), page=page):
        yield _row_or_skip(raw, "loki-archive")


def read_loki_live(until: datetime, *, page: int = _LOKI_PAGE) -> Iterator[AuditRow | Skipped]:
    """Rows of the live stream: lineage names since the cutover, every name for ~84 hours.

    The window opens at the archive's freeze point and runs to `until`, so the permanent
    lineage rows and the recent rows of every other audit name come from one read.
    """
    for raw in _loki_windows(ARCHIVE_FREEZE_AT, until, _loki_query(archive=False), page=page):
        yield _row_or_skip(raw, "loki-live")


def read_jsonl(directories: Iterable[Path], until: datetime) -> Iterator[AuditRow | Skipped]:
    """Audit rows from local event mirrors: `events-*.jsonl` and `events-*.lineage.jsonl`."""
    for directory in directories:
        files = sorted(
            [*directory.glob("events-????????.jsonl"), *directory.glob("events-*.lineage.jsonl")]
        )
        for path in files:
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
                    if raw.get("category") != "audit":
                        continue
                    row = _row_or_skip(raw, "jsonl")
                    if isinstance(row, AuditRow) and row.ts > until:
                        continue
                    yield row


def _row_or_skip(raw: dict[str, Any], imported_from: str) -> AuditRow | Skipped:
    try:
        return normalize(raw, imported_from=imported_from)
    except SkippedRowError as skipped:
        return Skipped(str(skipped))


def sources(
    jsonl_dirs: list[Path], until: datetime, *, page: int = _LOKI_PAGE
) -> list[tuple[str, Iterator[AuditRow | Skipped]]]:
    """The sources in precedence order: when two hold an event, the first one's row is kept."""
    return [
        ("loki-archive", read_loki_archive(until, page=page)),
        ("loki-live", read_loki_live(until, page=page)),
        ("jsonl", read_jsonl(jsonl_dirs, until)),
    ]


@dataclass
class Plan:
    """What a pass over the sources found, before touching the table."""

    read: Counter[str]
    skipped: Counter[tuple[str, str]]
    duplicates: Counter[str]
    by_name: Counter[tuple[str, str]]
    first_ts: datetime | None
    last_ts: datetime | None
    uids: set[int]


def plan(streams: list[tuple[str, Iterator[AuditRow | Skipped]]]) -> Plan:
    """Count what the sources hold, deduplicated by `event_uid` in precedence order."""
    found = Plan(Counter(), Counter(), Counter(), Counter(), None, None, set())
    for name, rows in streams:
        for row in rows:
            if isinstance(row, Skipped):
                found.skipped[(name, row.reason)] += 1
                continue
            found.read[name] += 1
            if row.event_uid in found.uids:
                found.duplicates[name] += 1
                continue
            found.uids.add(row.event_uid)
            found.by_name[(name, row.event_name)] += 1
            found.first_ts = row.ts if found.first_ts is None else min(found.first_ts, row.ts)
            found.last_ts = row.ts if found.last_ts is None else max(found.last_ts, row.ts)
    return found


def _already_present(conn: Any, uids: set[int]) -> int:
    present = 0
    ordered = sorted(uids)
    for start in range(0, len(ordered), 10_000):
        chunk = ordered[start : start + 10_000]
        row = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_uid = ANY(%s)", (chunk,)
        ).fetchone()
        present += int(row[0])
    return present


def render(found: Plan, present: int) -> str:
    """The reconciliation a dry-run prints."""
    lines = ["source          read  duplicate"]
    for name in ("loki-archive", "loki-live", "jsonl"):
        lines.append(f"{name:<14} {found.read[name]:>6} {found.duplicates[name]:>10}")
    for (name, reason), count in sorted(found.skipped.items()):
        lines.append(f"skipped {name}: {reason} x {count}")
    lines.append(f"distinct events: {len(found.uids)}")
    lines.append(f"already in audit_events: {present}")
    lines.append(f"would insert: {len(found.uids) - present}")
    if found.first_ts is not None and found.last_ts is not None:
        lines.append(f"span: {found.first_ts.isoformat()} .. {found.last_ts.isoformat()}")
    lines.append("by event name (source: count):")
    for (name, event), count in sorted(found.by_name.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        lines.append(f"  {event:<28} {name:<13} {count}")
    return "\n".join(lines)


def apply(streams: list[tuple[str, Iterator[AuditRow | Skipped]]]) -> tuple[int, int]:
    """Insert every distinct row in ONE transaction; returns `(inserted, already_present)`."""
    seen: set[int] = set()
    inserted = 0
    attempted = 0
    batch: list[tuple[Any, ...]] = []
    with write_transaction() as conn, conn.cursor() as cur:

        def flush() -> None:
            nonlocal inserted, batch
            if batch:
                cur.executemany(_INSERT, batch)
                inserted += cur.rowcount if cur.rowcount > 0 else 0
                batch = []

        for _name, rows in streams:
            for row in rows:
                if isinstance(row, Skipped) or row.event_uid in seen:
                    continue
                seen.add(row.event_uid)
                attempted += 1
                batch.append(row.params())
                if len(batch) >= _INSERT_BATCH:
                    flush()
        flush()
        present = _already_present(conn, seen)
        if present < len(seen):
            raise RuntimeError(f"{len(seen) - present} planned rows are missing after the insert")
    return inserted, attempted - inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    parser.add_argument(
        "--jsonl", action="append", default=[], type=Path, metavar="DIR", help="a local logs dir"
    )
    parser.add_argument(
        "--until",
        type=_timestamp,
        default=None,
        help="ISO-8601 upper bound (default: now); rows after it are left to the live path",
    )
    parser.add_argument(
        "--loki-page",
        type=_page_size,
        default=_LOKI_PAGE,
        metavar="N",
        help=f"Loki lines per page, 1..{_MAX_LOKI_PAGE} (default: {_LOKI_PAGE})",
    )
    args = parser.parse_args(argv)
    until = args.until or datetime.now(UTC)
    missing = [str(path) for path in args.jsonl if not path.is_dir()]
    if missing:
        print(f"error: --jsonl directory not found: {', '.join(missing)}")
        return 1

    found = plan(sources(args.jsonl, until, page=args.loki_page))
    with connect() as conn:
        present = _already_present(conn, found.uids)
    print(render(found, present))
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply to write)")
        return 0
    inserted, skipped_existing = apply(sources(args.jsonl, until, page=args.loki_page))
    print(f"\ninserted {inserted} rows ({skipped_existing} already present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
