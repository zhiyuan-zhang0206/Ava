"""Recover persisted metric observations outside the interactive request path.

The regular maintenance pass replays retained Loki and local JSONL mirrors.
Live-era sources use the emitter's stable identity, so their replay and live
projection commute. The frozen archive owns a disjoint timestamp range. A recorded scan means that source was traversed, not that upstream
collection was lossless. Mirrors on other runners can use the same CLI explicitly.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection

from services.events_maintenance.rollup import _query_budget
from shared.config import settings
from shared.loki_index_labels import (
    ARCHIVE_FLOOR_AT,
    ARCHIVE_FREEZE_AT,
    archive_stream_selector,
    escape_logql_label,
    event_stream_selector,
    retention_floor,
    split_index_label_window,
)
from shared.observability import cluster_label
from shared.observed_metrics import MetricObservation, observe_row, write_observations
from shared.paths import logs_dir
from shared.telemetry import event_id

_EVENTS = ["llm_usage", "turn_end", "exec", "exec_.+", "exec\\(.*", "node_exit"]
_PAGE_LIMIT = 5000
_PASS_SECONDS = 120.0


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Observed metrics recovery pass exhausted its time budget")
    return min(10.0, remaining)


def _fetch(logql: str, start_ns: int, end_ns: int, deadline: float) -> list[tuple[int, str]]:
    """One bounded background query; no count query and no interactive admission."""
    params = urllib.parse.urlencode(
        {
            "query": logql,
            "start": start_ns,
            "end": end_ns - 1,
            "limit": _PAGE_LIMIT,
            "direction": "forward",
        }
    )
    url = (
        settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range?" + params
    )
    with (
        _query_budget.slot(),
        urllib.request.urlopen(url, timeout=_remaining(deadline)) as response,  # noqa: S310 -- configured HTTP endpoint
    ):
        payload = json.load(response)
    return [
        (int(ts), line) for stream in payload["data"]["result"] for ts, line in stream["values"]
    ]


def _persist_lines(conn: Connection[Any], rows: list[tuple[int, str]]) -> int:
    observations: list[MetricObservation] = []
    for ts_ns, line in rows:
        row = json.loads(line)
        row["id"] = event_id(line, ts_ns)
        observation = observe_row(row)
        if observation is not None:
            observations.append(observation)
    with conn.transaction():
        return write_observations(observations, db=conn)


def _recover_range(
    conn: Connection[Any],
    logql: str,
    start_ns: int,
    end_ns: int,
    deadline: float,
) -> int:
    """Bisect full pages, refusing ambiguous equal-timestamp overflow."""
    rows = _fetch(logql, start_ns, end_ns, deadline)
    if len(rows) < _PAGE_LIMIT:
        return _persist_lines(conn, rows)
    if end_ns - start_ns <= 1:
        raise RuntimeError("Metric source exceeds the page limit at one timestamp; scan incomplete")
    middle = (start_ns + end_ns) // 2
    return _recover_range(conn, logql, start_ns, middle, deadline) + _recover_range(
        conn, logql, middle, end_ns, deadline
    )


def _record_scan(
    conn: Connection[Any],
    source: str,
    source_key: str,
    start: datetime,
    end: datetime,
) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO agent_metric_scans(source,source_key,window_start,window_end) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT(source,source_key,window_start,window_end) "
            "DO UPDATE SET scanned_at=now()",
            (source, source_key, start, end),
        )


def replay_loki(
    conn: Connection[Any],
    *,
    now: datetime,
    deadline: float,
    archive: bool = False,
) -> int:
    """Recover retained hours incrementally; rerun recent hours for late ingestion."""
    start = (
        ARCHIVE_FLOOR_AT
        if archive
        else max(retention_floor(now), ARCHIVE_FREEZE_AT + timedelta(microseconds=1))
    )
    end = min(now, ARCHIVE_FREEZE_AT + timedelta(microseconds=1)) if archive else now
    source = "archive_loki" if archive else "loki"
    source_key = settings.observability.telemetry_loki_url.rstrip("/")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT window_start,window_end FROM agent_metric_scans "
            "WHERE source=%s AND source_key=%s AND window_start>=%s",
            (source, source_key, start),
        )
        completed = set(cur.fetchall())
    conn.commit()
    count = 0
    while start < end:
        stop = min(start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1), end)
        if (start, stop) not in completed or stop > now - timedelta(hours=2):
            _remaining(deadline)
            if archive:
                pipelines = [(start, stop, archive_stream_selector())]
            else:
                pipelines = [
                    (
                        s.start,
                        s.end,
                        event_stream_selector(
                            era=s.era,
                            agent_id=None,
                            event_names=_EVENTS,
                            indexed_labeled=s.era.value == "indexed",
                        ),
                    )
                    for s in split_index_label_window(start, stop)
                ]
            for lo, hi, selector in pipelines:
                # Extract only the event name; full json extraction explodes
                # Loki series cardinality with trace/span labels.
                pattern = "|".join(_EVENTS).replace("\\", "\\\\")
                cluster = escape_logql_label(cluster_label())
                cluster_filter = (
                    f' | json metric_cluster="cluster" | metric_cluster="{cluster}" or metric_cluster=""'
                    if archive
                    else f' | cluster="{cluster}"'
                )
                query = (
                    selector
                    + cluster_filter
                    + ' | json metric_event="event_name" | metric_event=~"'
                    + pattern
                    + '"'
                )
                count += _recover_range(
                    conn, query, int(lo.timestamp() * 1e9), int(hi.timestamp() * 1e9), deadline
                )
            _record_scan(conn, source, source_key, start, stop)
        start = stop
    return count


def replay_jsonl(conn: Connection[Any], path: Path, *, deadline: float) -> int:
    """Replay an append-only mirror with an atomic fact/byte-position checkpoint."""
    source_key = f"{socket.gethostname()}:{path.resolve()}"
    file_stat = path.stat()
    identity = f"{file_stat.st_dev}:{file_stat.st_ino}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT identity,position FROM agent_metric_file_cursors WHERE source_key=%s",
            (source_key,),
        )
        previous = cur.fetchone()
    conn.commit()
    position = (
        previous[1]
        if previous and previous[0] == identity and previous[1] <= file_stat.st_size
        else 0
    )
    count = 0
    lower: datetime | None = None
    upper: datetime | None = None
    # Binary offsets are portable across UTF-8 widths. A partially appended
    # final line is left for the next pass, never acknowledged as parsed.
    with path.open("rb") as source:
        source.seek(position)
        while source.tell() < file_stat.st_size:
            _remaining(deadline)
            batch: list[MetricObservation] = []
            consumed = 0
            excluded_archive = 0
            for _ in range(500):
                before = source.tell()
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    source.seek(before)
                    break
                row = json.loads(line)
                stamp = datetime.fromisoformat(row["ts"])
                if stamp.tzinfo is None:
                    raise ValueError("Recovery source timestamp must be timezone-aware")
                if stamp <= ARCHIVE_FREEZE_AT:
                    # Archive owns this timestamp regardless of historical
                    # payload shape. Persist the exclusion with byte progress.
                    excluded_archive += 1
                    consumed += 1
                    continue
                if "id" not in row:
                    # Before Aug 23 the mirror wrote this exact canonical body
                    # without persisting the shared Loki surrogate (1a25ddb90).
                    row["id"] = event_id(line.decode().rstrip("\r\n"), int(stamp.timestamp() * 1e9))
                observation = observe_row(row)
                if observation is not None:
                    lower = (
                        observation.occurred_at
                        if lower is None
                        else min(lower, observation.occurred_at)
                    )
                    upper = (
                        observation.occurred_at
                        if upper is None
                        else max(upper, observation.occurred_at)
                    )
                    batch.append(observation)
                consumed += 1
            if not consumed:
                break
            with conn.transaction():
                count += write_observations(batch, db=conn)
                conn.execute(
                    "INSERT INTO agent_metric_file_cursors(source_key,identity,position,excluded_archive_rows) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT(source_key) DO UPDATE SET "
                    "identity=EXCLUDED.identity,position=EXCLUDED.position, "
                    "excluded_archive_rows=CASE WHEN agent_metric_file_cursors.identity=EXCLUDED.identity "
                    "THEN agent_metric_file_cursors.excluded_archive_rows ELSE 0 END + EXCLUDED.excluded_archive_rows",
                    (source_key, identity, source.tell(), excluded_archive),
                )
    if lower is not None and upper is not None:
        _record_scan(
            conn,
            "rollup_jsonl" if ".rollup." in path.name else "full_jsonl",
            source_key,
            lower,
            upper + timedelta(microseconds=1),
        )
    return count


def recover_observations(conn: Connection[Any], *, now: datetime | None = None) -> int:
    """One bounded maintenance pass; failures preserve already committed batches."""
    now = now or datetime.now(UTC)
    deadline = time.monotonic() + _PASS_SECONDS
    count = 0
    failures: list[Exception] = []
    try:
        count += replay_loki(conn, now=now, deadline=deadline - _PASS_SECONDS / 2)
    except Exception as exc:
        conn.rollback()
        failures.append(exc)
    # A Loki outage must not block its independent local recovery source.
    for path in sorted(logs_dir().glob("events-????????*.jsonl"), reverse=True):
        if ".lineage." in path.name:
            continue
        try:
            count += replay_jsonl(conn, path, deadline=deadline)
        except Exception as exc:
            conn.rollback()
            failures.append(exc)
            if time.monotonic() >= deadline:
                break
    if failures:
        raise ExceptionGroup("Observed metric recovery was incomplete", failures)
    return count


def main() -> None:
    """Operator-controlled recovery; this command never changes retained sources."""
    from shared.db import connect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", type=Path, action="append", default=[])
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    with connect() as conn:
        deadline = time.monotonic() + _PASS_SECONDS
        if args.jsonl:
            for path in args.jsonl:
                replay_jsonl(conn, path, deadline=deadline)
        else:
            replay_loki(conn, now=datetime.now(UTC), deadline=deadline, archive=args.archive)


if __name__ == "__main__":
    main()
